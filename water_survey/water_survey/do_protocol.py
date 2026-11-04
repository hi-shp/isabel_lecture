#!/usr/bin/env python3
"""Read WL118DO snapshots over Linux Bluetooth Classic RFCOMM.

Uses only Python's standard library. Sends N (identity), V (firmware),
and S (snapshot). Never changes calibration or starts a measurement stream.
UTC timestamps are computer request/receive times, not sensor sample times.
"""
import argparse
import base64
import csv
import datetime as dt
import json
import math
from pathlib import Path
import re
import socket
import struct
import subprocess
import time


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def request_frame(command):
    if command not in "NVS":
        raise ValueError("Only identity, firmware and snapshot reads are allowed")
    body = command.encode("ascii") + bytes(10)
    return b"{{" + base64.b64encode(body + bytes([sum(body) & 255])) + b"}}"


class FrameParser:
    def __init__(self):
        self.buffer = bytearray()
        self.invalid = 0

    def feed(self, data):
        self.buffer.extend(data)
        results = []
        while True:
            start = self.buffer.find(b"{{")
            if start < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer.endswith(b"{") else b""
                break
            del self.buffer[:start]
            end = self.buffer.find(b"}}", 2)
            if end < 0:
                if len(self.buffer) > 256:
                    self.invalid += 1
                    del self.buffer[:2]
                    continue
                break
            encoded = bytes(self.buffer[2:end])
            del self.buffer[:end + 2]
            try:
                body = base64.b64decode(encoded, validate=True)
            except ValueError:
                self.invalid += 1
                continue
            if len(body) != 12 or sum(body[:11]) & 255 != body[11]:
                self.invalid += 1
                continue
            results.append(body)
        return results


def decode_snapshot(body):
    kind = chr(body[0])
    offset = {"s": 2, "t": 4, "u": 3}.get(kind)
    if offset is None:
        raise ValueError("Not a snapshot response")
    value = struct.unpack_from("<f", body, offset)[0]
    if not math.isfinite(value):
        raise ValueError("Non-finite sensor value")
    battery = body[3] if kind == "t" else body[2] if kind == "u" else ""
    return value, battery


def discover_channel(address):
    """Establish the ACL link via SDP before opening the SPP channel."""
    result = subprocess.run(["sdptool", "browse", address],
                            capture_output=True, text=True, timeout=25)
    match = re.search(r'"RFCOMM"\s*\(0x0003\)\s*Channel:\s*(\d+)', result.stdout)
    if result.returncode != 0 or not match:
        detail = (result.stdout + result.stderr).strip()[:600]
        raise ConnectionError("Sensor SPP service unavailable: " + detail)
    return int(match.group(1))


def open_sensor(address, expected_channel):
    for attempt in range(1, 4):
        sock = None
        try:
            channel = discover_channel(address)
            if channel != expected_channel:
                raise ValueError(f"Expected RFCOMM channel {expected_channel}, SDP reported {channel}")
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            sock.settimeout(12)
            sock.connect((address, channel))
            return sock, attempt
        except (OSError, subprocess.TimeoutExpired) as exc:
            if sock is not None:
                sock.close()
            print(f"Connection attempt {attempt}/3: {exc}", flush=True)
            if attempt == 3:
                raise
            time.sleep(2)


def exchange(sock, parser, command, accepted, raw):
    sent = utc_now()
    begin = time.monotonic()
    frame = request_frame(command)
    raw.write(json.dumps({"utc": sent, "direction": "tx", "hex": frame.hex()}) + "\n")
    raw.flush()
    sock.sendall(frame)
    deadline = begin + 5
    while time.monotonic() < deadline:
        sock.settimeout(max(.01, deadline - time.monotonic()))
        data = sock.recv(1024)
        received = utc_now()
        if not data:
            raise ConnectionError("Sensor closed connection")
        raw.write(json.dumps({"utc": received, "direction": "rx", "hex": data.hex()}) + "\n")
        raw.flush()
        for body in parser.feed(data):
            if chr(body[0]) in accepted:
                return body, sent, received, round((time.monotonic() - begin) * 1000, 3)
    raise TimeoutError("No matching response")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--address", default="22:61:F4:4C:10:58")
    ap.add_argument("--channel", type=int, default=2)
    ap.add_argument("--samples", type=int, default=60, help="0 runs until Ctrl+C")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--output", type=Path, default=Path("logs"))
    args = ap.parse_args()
    if args.samples < 0 or args.interval < .1:
        ap.error("samples must be >= 0 and interval >= 0.1 seconds")
    args.output.mkdir(parents=True, exist_ok=True)
    run = args.output / dt.datetime.now(dt.timezone.utc).strftime("do_%Y%m%dT%H%M%S_%fZ")
    metadata = {"address": args.address, "channel": args.channel,
                "started_utc": utc_now(), "interval_seconds": args.interval,
                "timestamp_basis": "host UTC request and receive time",
                "unit": "mg/L", "status": "connecting", "samples": 0}
    parser = FrameParser()
    fields = ["request_utc", "received_utc", "elapsed_seconds", "sequence",
              "sensor_name", "do_mg_l", "battery_raw", "response_ms",
              "within_spec_range", "response_type", "payload_hex"]
    try:
        connection, attempts = open_sensor(args.address, args.channel)
        metadata["connection_attempts"] = attempts
        metadata["sdp_verified"] = True
        with connection as sock, \
                run.with_suffix(".jsonl").open("x", encoding="utf-8") as raw, \
                run.with_suffix(".csv").open("x", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fields)
            writer.writeheader()
            name = exchange(sock, parser, "N", "n", raw)[0][2:11].rstrip(b"\0").decode("ascii")
            metadata["sensor_name"] = name
            if not name.lower().startswith("wl118d"):
                raise ValueError("Unexpected sensor identity: " + repr(name))
            firmware = exchange(sock, parser, "V", "v", raw)[0][2:11].rstrip(b"\0").decode("ascii")
            metadata["firmware"] = firmware
            print(json.dumps({"connected": True, "name": name, "firmware": firmware}), flush=True)
            started = time.monotonic()
            while args.samples == 0 or metadata["samples"] < args.samples:
                target = started + metadata["samples"] * args.interval
                time.sleep(max(0, target - time.monotonic()))
                body, sent, received, latency = exchange(sock, parser, "S", "stu", raw)
                value, battery = decode_snapshot(body)
                metadata["samples"] += 1
                row = dict(request_utc=sent, received_utc=received,
                           elapsed_seconds=round(time.monotonic() - started, 3),
                           sequence=metadata["samples"], sensor_name=name,
                           do_mg_l=round(value, 6), battery_raw=battery, response_ms=latency,
                           within_spec_range=0 <= value <= 50, response_type=chr(body[0]),
                           payload_hex=body.hex())
                writer.writerow(row)
                csvfile.flush()
                print(f"{received} DO={value:.2f} mg/L ({metadata['samples']})", flush=True)
            metadata["status"] = "complete"
    except KeyboardInterrupt:
        metadata["status"] = "stopped_by_user"
    except Exception as exc:
        metadata.update(status="error", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        metadata.update(finished_utc=utc_now(), invalid_frames=parser.invalid)
        run.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        print("Saved:", str(run), flush=True)


if __name__ == "__main__":
    main()
