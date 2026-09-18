#!/usr/bin/env python3
"""Decode raw SD-card data dumps from a BMC RESmart GII CPAP machine.

Reverse-engineered, unofficial, not for medical use. See README.md for the
packet-format spec and DESIGN.md for architecture details.
"""

import struct
import sys
import glob
import argparse
import datetime


PACKET_SIZE = 256
# maximum number of csv lines buffered before writing them to the output file
FLUSH_LINES = 4096


class packet(object):
    """ Holds data for one 256-byte packet from the raw (numerical
    extension) RESmart data files. Also methods for parsing the data """

    # length of data fields (uint16_t), not counting zero pads and timestamp
    dlen = 106

    timestamp_fields = ["year", "month", "day", "hour", "minute", "second", "?"]

    # human-readable labels for data words. These are identical for every
    # packet, so they are built once here (class level) instead of once per
    # packet, which made parsing the full dump ~10x slower.
    data_fields = ["?" for _ in range(dlen)]

    # first 85 fields are 25 Hz and 10 Hz measurements of pressure/flow
    for _i in range(25):
        data_fields[4 + _i]           = "resA_{:d}".format(_i)
        data_fields[4 + _i + 25]      = "resB_{:d}".format(_i)
        data_fields[4 + _i + 50]      = "resC_{:d}".format(_i)
        if _i < 10:
            data_fields[4 + _i + 75]  = "pulse_{:d}".format(_i)
    del _i

    # some data fields are known, label them
    known_fields = {
        "usage_day": 1,
        "IPAP": 2,
        "EPAP": 3,
        "tidal_vol": 99,
        "spO2_pct": 102,
        "HR_BPM": 103,
        "rep_rate": 104}

    # units of the raw stored values, shown in the CSV header row.
    # The raw words are NOT converted: the unit documents what the stored
    # value means (e.g. IPAP=13 means 6.5 cmH2O).
    known_units = {
        "usage_day": "",
        "IPAP": "0.5 cmH2O",
        "EPAP": "0.5 cmH2O",
        "tidal_vol": "L/min",
        "spO2_pct": "%",
        "HR_BPM": "bpm",
        "rep_rate": "breaths/min"}

    for _key, _val in known_fields.items():
        data_fields[_val] = _key
    del _key, _val

    def __init__(self, pbuf):
        """ given pbuf bytes, parse out the data"""
        assert len(pbuf) >= PACKET_SIZE
        self.parse_timestamp(pbuf)
        self.parse_data(pbuf)
        self.has_pulse = self.data[self.known_fields["spO2_pct"]] > 0

    def parse_timestamp(self, pbuf):
        """ the timestamp is the last 8 bytes of every packet"""
        self.timestamp = struct.unpack("HBBBBBB",
                                       pbuf[PACKET_SIZE-8:PACKET_SIZE])

        # for readability and convenience, parse out individual fields.
        self.year = self.timestamp[0]
        self.month = self.timestamp[1]
        self.day = self.timestamp[2]
        self.hour = int(self.timestamp[3])
        self.minute = self.timestamp[4]
        self.second = self.timestamp[5]

        self.date = datetime.date(self.year, self.month, self.day)
        self.ordinal = self.date.toordinal()
        self.datestr = self.date.isoformat()

    def parse_data(self, pbuf):
        """  extract all 16-bit uint16_t data (dlen words) from the packet.
        A single bulk unpack instead of one struct.unpack per word."""
        self.data = list(struct.unpack(str(self.dlen) + "H",
                                       pbuf[:2*self.dlen]))

    def get_known_values_csv(self):
        # print only understood values
        outstr = ""
        for val in self.known_fields.values():
            outstr += "{}, ".format(self.data[val])
        return outstr

    def get_all_values_csv(self):
        # print all data values whether we know what they are or not
        outstr = ""
        for i in range(self.dlen):
            outstr += "{}, ".format(self.data[i])
        return outstr

    def fix_csv(self, csv_str):
        # remove trailing spaces & comma
        csv_str = csv_str.strip()
        if csv_str[-1] == ',':
            csv_str = csv_str[0:-1]
        return csv_str

    def get_time_ymd_csv(self):
        # return time string in year, month, day, hour, minute, second, ? format
        outstr = ""
        for i in self.timestamp:
            outstr += "{:d}, ".format(i)
        return outstr

    def get_time_seconds(self):
        return self.second + 60*self.minute + 3600*(self.hour + 24*(self.ordinal))

    def get_timestamp_iso(self, subsec=None):
        """ISO 8601 timestamp for this packet, optionally shifted by subsec
        fractional seconds (used for 10/25 Hz subsamples)"""
        ts = datetime.datetime(self.year, self.month, self.day,
                               self.hour, self.minute, self.second)
        if subsec is not None:
            ts += datetime.timedelta(seconds=subsec)
            return ts.isoformat(sep='T', timespec='milliseconds')
        return ts.isoformat(sep='T', timespec='seconds')

    def get_10hz_csv(self, i):
        return "{}, ".format(self.data[4 + 75 + i])

    def get_25hz_csv(self, i):
        # These fields are arrays of 25 values/second, something to do with
        # respiration. Last one is cleanest -- filtered?
        outstr = "{:d}, {:d}, {:d}, ".format(self.data[4 + i],
                                             self.data[4 + 25 + i],
                                             self.data[4 + 50 + i])
        return outstr


def s2HMS(seconds):
    # return a string giving hours and minutes from seconds
    hours = int(seconds/3600.)
    minutes = int((seconds % 3600)/60.)
    return "{:02d}:{:02d}".format(hours, minutes)


def make_header(args):
    """ csv header row matching the column layout of the current output mode.
    Only class-level constants are used, so the header can be written before
    any packet has been parsed."""
    def named(name):
        unit = packet.known_units.get(name, "")
        return name + (" ({})".format(unit) if unit else "")

    cols = []
    # time columns
    if args.time_ymd:
        cols += packet.timestamp_fields
    elif args.time_seconds:
        cols += ["time_seconds"]
    else:
        cols += ["timestamp"]

    # data columns
    if args.all_data:
        for i in range(packet.dlen):
            label = packet.data_fields[i]
            if label == "?":
                cols.append("word_{:03d}".format(i))
            else:
                cols.append(named(label))
    else:
        for name in packet.known_fields:
            cols.append(named(name))

    # sub-second sample columns
    if args.f10_hz:
        if args.time_ymd or args.time_seconds:
            cols.append("time_frac")
        cols.append("pulse")
    elif args.f25_hz:
        if args.time_ymd or args.time_seconds:
            cols.append("time_frac")
        cols += ["resA", "resB", "resC"]

    return ", ".join(cols)


def packet_rows(p, args):
    """ return the list of csv rows (without trailing newline) for one packet,
    following the selected output mode """
    if args.all_data:
        data = p.get_all_values_csv()
    else:
        data = p.get_known_values_csv()

    if args.time_seconds or args.time_ymd:
        # numeric time column(s) repeated on every sub-second row
        if args.time_seconds:
            tbase = "{}, ".format(p.get_time_seconds())
        else:
            tbase = p.get_time_ymd_csv()
        if args.f10_hz:
            return [p.fix_csv(tbase + data +
                              "{:.2f}, ".format(float(p.get_time_seconds()) + float(j)/10.) +
                              p.get_10hz_csv(j)) for j in range(10)]
        elif args.f25_hz:
            return [p.fix_csv(tbase + data +
                              "{:.2f}, ".format(float(p.get_time_seconds()) + float(j)/25.) +
                              p.get_25hz_csv(j)) for j in range(25)]
        else:
            return [p.fix_csv(tbase + data)]
    else:
        # single ISO timestamp column, sub-second precision for high-freq rows
        if args.f10_hz:
            return [p.fix_csv(p.get_timestamp_iso(float(j)/10.) + ", " +
                              data + p.get_10hz_csv(j)) for j in range(10)]
        elif args.f25_hz:
            return [p.fix_csv(p.get_timestamp_iso(float(j)/25.) + ", " +
                              data + p.get_25hz_csv(j)) for j in range(25)]
        else:
            return [p.fix_csv(p.get_timestamp_iso() + ", " + data)]


class day_tally(object):
    """ streaming aggregation of one day's packets for --info """

    def __init__(self, date):
        self.date = date
        self.secs = 0
        self.hours = ["." for _ in range(24)]
        self.hour = -1
        self.has_pulse = False

    def add(self, p):
        self.secs += 1
        if p.has_pulse:
            self.has_pulse = True
        if p.hour > self.hour:
            self.hour = p.hour
            self.hours[self.hour] = 'O' if self.has_pulse else '+'
            self.has_pulse = False

    def render(self):
        # one char per hour: '.' no data, '+' flow data, 'O' pulse oximeter
        return "{} {} {}\n".format(self.date.isoformat(),
                                   "".join(self.hours),
                                   s2HMS(self.secs))


def iter_packets(databuff):
    """ yield a packet for each 256-byte block in the buffer """
    for off in range(0, len(databuff) - PACKET_SIZE, PACKET_SIZE):
        yield packet(databuff[off:off+PACKET_SIZE])


def date_bounds(databuff):
    """ exact (min, max) packet dates in a file, cheap timestamp-only pass """
    lo = None
    hi = None
    for off in range(0, len(databuff) - PACKET_SIZE, PACKET_SIZE):
        y, m, d = struct.unpack("HBBBBB",
                                databuff[off+PACKET_SIZE-8:off+PACKET_SIZE-1])[:3]
        try:
            dt = datetime.date(y, m, d)
        except ValueError:
            continue
        if lo is None or dt < lo:
            lo = dt
        if hi is None or dt > hi:
            hi = dt
    return lo, hi


def note_new_day(p, start_date, args):
    """ print progress when a new date is first seen while reading """
    if not args.quiet:
        if start_date is not None and p.date == start_date:
            print("Found start date {}.".format(start_date.isoformat()))
        print("reading data from {}".format(p.datestr))


def do_info(files, args):
    """ --info: print a day-by-day summary, read only, never writes a file """
    tally = None
    outlines = []
    parsed = 0
    last_ord = None
    for datafile in files:
        with open(datafile, "rb") as f:
            databuff = f.read()
        for p in iter_packets(databuff):
            parsed += 1
            if last_ord != p.ordinal:
                last_ord = p.ordinal
                note_new_day(p, None, args)
            if tally is None or tally.date != p.date:
                if tally is not None:
                    outlines.append(tally.render())
                tally = day_tally(p.date)
            tally.add(p)
    if tally is not None:
        outlines.append(tally.render())

    if not args.quiet:
        print("{:d} packets found in {} files".format(parsed, len(files)))
    sys.stdout.write("".join(outlines))
    return 0


def do_write(files, args, start_date, end_date):
    """ stream packets to the csv file: header first, then one buffered row
    per packet. Packets outside the requested range are dropped as they are
    read, and whole files outside the range are skipped entirely."""
    parsed = 0
    written = 0
    last_read_ord = None
    last_write_ord = None
    buffer = []

    def flush():
        if buffer:
            outf.writelines(buffer)
            buffer[:] = []

    with open(args.output, 'w') as outf:
        outf.write(make_header(args) + "\n")

        for datafile in files:
            with open(datafile, "rb") as f:
                databuff = f.read()

            if start_date is not None:
                lo, hi = date_bounds(databuff)
                if lo is None or hi < start_date or lo > end_date:
                    continue  # whole file outside the requested range

            for p in iter_packets(databuff):
                parsed += 1
                if last_read_ord != p.ordinal:
                    last_read_ord = p.ordinal
                    note_new_day(p, start_date, args)

                if start_date is not None and \
                   not (start_date <= p.date <= end_date):
                    continue  # out of range packet, drop it

                if not args.quiet and p.ordinal != last_write_ord:
                    last_write_ord = p.ordinal
                    print("Writing {} data to {}".format(p.datestr,
                                                         args.output))

                rows = packet_rows(p, args)
                buffer.extend(row + "\n" for row in rows)
                written += len(rows)
                if len(buffer) >= FLUSH_LINES:
                    flush()
        flush()

    if not args.quiet:
        print("{:d} packets found in {} files".format(parsed, len(files)))
    if start_date is not None and written == 0:
        print("No data found in range {} to {}.".format(
              start_date.isoformat(), end_date.isoformat()), file=sys.stderr)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description='Extract data from BMC RESmart raw data files')

    parser.add_argument('--info', '-i',
                        action='store_true',
                        help='Prints readable summary of data and dates to stdout.')

    parser.add_argument('--f25_hz', '-2',
                        action='store_true',
                        help='Print out all 25 Hz (flow) data. this will make output files 25x as big.')

    parser.add_argument('--f10_hz', '-1',
                        action='store_true',
                        help='Print out all 10 Hz (pulse) data. this will make output files 10x as big.')

    parser.add_argument('--all_data', '-a',
                        action='store_true',
                        help='Print out all 1Hz data fields known or unknown')

    parser.add_argument('--time_ymd', '-y',
                        action='store_true',
                        help='Print timestamp in Y, M, D, H, M, S format')

    parser.add_argument('--time_seconds', '-s',
                        action='store_true',
                        help='Print timestamp in seconds since beginning of year')

    parser.add_argument('--quiet', '-q',
                        action='store_true',
                        help='Do not print progress and info to stderr')

    parser.add_argument('--output', '-o',
                        help='Output data CSV file (default: %(default)s); it overwrites existing data.',
                        default='RESmart_data.csv')

    parser.add_argument('--dates', '-d', nargs='+',
                        help='select date range in YYYY-MM-DD format. Single date is one day, two dates are start and end of time range.',
                        default=[])

    return parser


def parse_dates(args, parser):
    # returns (start_date, end_date); None,None when -d was not given
    start_date = None
    end_date = None
    if len(args.dates) > 2:
        parser.error('-d requires 1 or 2 dates (YYYY-MM-DD), got {}'.format(len(args.dates)))
    if len(args.dates) > 0:
        try:
            start_date = datetime.datetime.strptime(args.dates[0], '%Y-%m-%d').date()
        except ValueError:
            parser.error("Incorrect -d date format '{}', should be YYYY-MM-DD".format(args.dates[0]))
    if len(args.dates) > 1:
        try:
            end_date = datetime.datetime.strptime(args.dates[1], '%Y-%m-%d').date()
        except ValueError:
            parser.error("Incorrect -d date format '{}', should be YYYY-MM-DD".format(args.dates[1]))
    if start_date is not None and end_date is None:
        # a single date means exactly that day
        end_date = start_date
    return start_date, end_date


def main():
    if sys.version_info.major < 3:
        print("sorry, requires Python 3.")
        sys.exit(1)

    parser = build_parser()

    # no arguments at all: show help and exit without reading or writing any files
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        parser.exit(2)

    args = parser.parse_args()
    start_date, end_date = parse_dates(args, parser)

    # Should probably ensure these files all have the same root...
    files = glob.glob('*.[0-9][0-9][0-9]')
    files.sort()

    if not files:
        print("No raw data files (*.nnn) found in current directory",
              file=sys.stderr)
        sys.exit(1)

    if args.info:
        return do_info(files, args)

    return do_write(files, args, start_date, end_date)


if __name__ == '__main__':
    sys.exit(main())