#!/usr/bin/env python3
"""
Extract centerline and optional wall coordinates from Assetto Corsa fast_lane.ai files.
"""
import argparse
import glob
import struct
import os
import json
import math
import sys

def slideVec2d(point, angle=0, offset=0):
    # same convention as original: angle in degrees, z uses negative sin
    x = point[0] + math.cos(angle * math.pi / 180.0) * offset
    z = point[2] - math.sin(angle * math.pi / 180.0) * offset
    return (x, point[1], z)

class TrackDetailNode:
    def __init__(self, rawIdeal, prevRawIdeal, rawDetail):
        # rawIdeal: (x, y, z, distance, id)
        self.id = rawIdeal[4]
        self.position = (rawIdeal[0], rawIdeal[1], rawIdeal[2])
        self.distance = rawIdeal[3]
        self.direction = -math.degrees(
            math.atan2(prevRawIdeal[2] - self.position[1],
                       self.position[0] - prevRawIdeal[0])
        )
        # rawDetail contains many floats; original used indices 5 and 6 for wall offsets
        try:
            _wallLeft = rawDetail[5]
            _wallRight = rawDetail[6]
            self.wallLeft = slideVec2d(self.position, -self.direction + 90, _wallLeft)
            self.wallRight = slideVec2d(self.position, -self.direction - 90, _wallRight)
            self.trackCenter = ((self.wallLeft[0] + self.wallRight[0]) / 2.0,
                                (self.wallLeft[1] + self.wallRight[1]) / 2.0,
                                (self.wallLeft[2] + self.wallRight[2]) / 2.0)
        except Exception:
            self.wallLeft = None
            self.wallRight = None
            self.trackCenter = self.position

def getNodesFromFastLane(fai_file):
    nodes = []
    try:
        with open(fai_file, "rb") as buffer:
            # header: 4 ints (header, length, lapTime, sampleCount)
            header_data = buffer.read(4 * 4)
            if len(header_data) < 16:
                raise ValueError("File too short or invalid header.")
            header, length, lapTime, sampleCount = struct.unpack("4i", header_data)

            # read "rawIdeal" entries: length entries of (4 floats, 1 int) == 5 items, each 4 bytes
            rawIdeal = []
            for i in range(length):
                rec = buffer.read(4 * 5)
                if len(rec) < 20:
                    raise ValueError("Unexpected end of file while reading rawIdeal.")
                # 4 floats and 1 int
                a, b, c, d, ident = struct.unpack("4f i", rec)
                rawIdeal.append((a, b, c, d, ident))

            # read extraCount and then that many rawDetail blocks (18 floats each in original)
            extraCountBytes = buffer.read(4)
            if len(extraCountBytes) < 4:
                extraCount = 0
            else:
                extraCount = struct.unpack("i", extraCountBytes)[0]

            for i in range(extraCount):
                prevRawIdeal = rawIdeal[i - 1] if i > 0 else rawIdeal[length - 1]
                try:
                    # read 18 floats (72 bytes)
                    rawDetailBytes = buffer.read(4 * 18)
                    if len(rawDetailBytes) < 4 * 18:
                        # fallback: try to continue with zeros
                        rawDetail = tuple([0.0] * 18)
                    else:
                        rawDetail = struct.unpack("18f", rawDetailBytes)
                    node = TrackDetailNode(rawIdeal[i], prevRawIdeal, rawDetail)
                    nodes.append(node)
                except Exception:
                    # ignore invalid node but continue
                    continue

        return nodes
    except Exception as e:
        raise RuntimeError("Failed to parse '{}': {}".format(fai_file, e))

def nodes_to_dicts(nodes, include_walls=False, subsample=1):
    out = []
    for idx, n in enumerate(nodes):
        if idx % subsample != 0:
            continue
        
        x, y, z = n.position
        x, z = z, -x  # rotate 90° CCW around Y

        d = {
            "index": idx,
            "id": n.id,
            "x": x,
            "y": y,
            "z": z,
            "distance": n.distance,
            "direction": n.direction
        }

        if include_walls:
            if n.wallLeft is not None:
                wx, wy, wz = n.wallLeft
                wx, wz = wz, -wx
                d.update({
                    "wallLeft_x": wx,
                    "wallLeft_y": wy,
                    "wallLeft_z": wz,
                })
            else:
                d.update({"wallLeft_x": None, "wallLeft_y": None, "wallLeft_z": None})

            if n.wallRight is not None:
                wx, wy, wz = n.wallRight
                wx, wz = wz, -wx
                d.update({
                    "wallRight_x": wx,
                    "wallRight_y": wy,
                    "wallRight_z": wz,
                })
            else:
                d.update({"wallRight_x": None, "wallRight_y": None, "wallRight_z": None})

        out.append(d)
    return out

def write_csv(dicts, out_file):
    import csv
    if not dicts:
        print("No data to write.")
        return
    keys = list(dicts[0].keys())
    with open(out_file, "w", newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in dicts:
            w.writerow(row)

def write_json(dicts, out_file):
    with open(out_file, "w") as f:
        json.dump(dicts, f, indent=2)

def write_txt(dicts, out_file):
    with open(out_file, "w") as f:
        for d in dicts:
            # default: x,z pairs
            f.write("{0:.6f},{1:.6f}\n".format(d['x'], d['z']))

def main():
    parser = argparse.ArgumentParser(description="Extract coordinates from Assetto Corsa fast_lane.ai files.")
    parser.add_argument("fastlane", help="Path to fast_lane.ai (supports glob patterns)")
    parser.add_argument("-o", "--out", help="Output file (default: stdout). If multiple input files, appends filename to out if directory omitted.")
    parser.add_argument("--format", choices=["csv","json","txt"], default="csv", help="Output format")
    parser.add_argument("--walls", action="store_true", help="Include wallLeft/wallRight coordinates")
    parser.add_argument("--subsample", type=int, default=1, help="Keep every Nth point (default 1 = keep all)")
    parser.add_argument("--stdout", action="store_true", help="Print output to stdout instead of file")
    args = parser.parse_args()

    matches = glob.glob(args.fastlane)
    if not matches:
        print("No files matched:", args.fastlane, file=sys.stderr)
        sys.exit(2)

    for i, path in enumerate(sorted(matches)):
        try:
            nodes = getNodesFromFastLane(path)
        except Exception as e:
            print(e, file=sys.stderr)
            continue

        dicts = nodes_to_dicts(nodes, include_walls=args.walls, subsample=max(1, args.subsample))

        # decide output path/behavior
        if args.stdout or (args.out is None and len(matches) == 1):
            if args.format == "json":
                print(json.dumps(dicts, indent=2))
            elif args.format == "csv":
                # print CSV to stdout
                import csv, sys as _sys
                if dicts:
                    keys = list(dicts[0].keys())
                    w = csv.DictWriter(_sys.stdout, fieldnames=keys)
                    w.writeheader()
                    for row in dicts:
                        w.writerow(row)
            else:  # txt
                for d in dicts:
                    print("{0:.6f},{1:.6f}".format(d['x'], d['z']))
        else:
            out = args.out
            # if multiple inputs and out is a dir or missing extension, write to directory
            if len(matches) > 1:
                if out is None:
                    out_dir = os.getcwd()
                elif os.path.isdir(out) or out.endswith(os.sep):
                    out_dir = out
                else:
                    # if user gave a filename but multiple inputs, create directory next to it
                    out_dir = out
                os.makedirs(out_dir, exist_ok=True)
                base = os.path.splitext(os.path.basename(path))[0]
                out_file = os.path.join(out_dir, "{}_coords.{}".format(base, args.format))
            else:
                if out is None:
                    out_file = os.path.splitext(path)[0] + "_coords." + args.format
                else:
                    out_file = out

            if args.format == "json":
                write_json(dicts, out_file)
            elif args.format == "csv":
                write_csv(dicts, out_file)
            else:
                write_txt(dicts, out_file)
            print("Wrote:", out_file)

if __name__ == "__main__":
    main()
