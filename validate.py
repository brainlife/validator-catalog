#!/usr/bin/env python3
"""brainlife.io validator for datatypes that have no dedicated validator.

Run by brainlife in the validator task directory:

    validate.py [--rules rules.json]

Reads config.json (file id -> uploaded path; _outputs[0] holds the datatype id, tags and meta),
checks each file against the datatype's file list (rules.json, generated from the abcd-spec
datatype catalog; formats come from the file names),
links every file into output/, and writes product.json. Errors fail the upload: a required file
missing, or a file that is unreadable or not in its format. Everything else is a warning. A bug
in this script never blocks an upload: it becomes a warning and the files are still linked.
Python standard library only.
"""
import argparse
import csv
import gzip
import json
import os
import pathlib
import struct
import sys
import traceback
import zlib

HERE = pathlib.Path(__file__).resolve().parent
CHUNK = 1 << 20

# Longest suffix first: (suffix, format)
SUFFIXES = [
    (".ome.zarr", "zarr"), (".nii.gz", "nifti-gz"), (".tsv.gz", "gzip"), (".zarr", "zarr"),
    (".nii", "nifti"), (".json", "json"), (".tsv", "tsv"), (".csv", "csv"),
    (".bvals", "bval"), (".bval", "bval"), (".bvecs", "bvec"), (".bvec", "bvec"),
    (".mgz", "mgz"), (".mgh", "mgh"), (".gii", "gifti"), (".png", "png"), (".jpg", "jpeg"),
    (".jpeg", "jpeg"), (".svg", "svg"), (".pdf", "pdf"), (".html", "html"), (".h5", "hdf5"),
    (".hdf5", "hdf5"), (".trx", "zip"), (".zip", "zip"), (".mat", "mat"), (".set", "mat"),
    (".edf", "edf"), (".bdf", "bdf"), (".fif", "fif"), (".vhdr", "vhdr"), (".tck", "tck"),
    (".trk", "trk"), (".gz", "gzip"),
]
MAGIC = {
    "png": [b"\x89PNG\r\n\x1a\n"], "jpeg": [b"\xff\xd8\xff"], "pdf": [b"%PDF"],
    "zip": [b"PK\x03\x04", b"PK\x05\x06"], "hdf5": [b"\x89HDF\r\n\x1a\n"], "edf": [b"0       "],
    "bdf": [b"\xffBIOSEMI"], "fif": [b"\x00\x00\x00\x64"], "tck": [b"mrtrix tracks"], "trk": [b"TRACK"],
    "vhdr": [b"Brain Vision", b"BrainVision", b"\xef\xbb\xbfBrain"],
}
TEXT_MARKERS = {"svg": b"<svg", "html": b"<html", "gifti": b"<GIFTI"}


def format_for(name, is_dir):
    lower = name.lower()
    for suffix, fmt in SUFFIXES:
        if lower.endswith(suffix):
            return fmt
    if lower in ("bvals", "bval"):
        return "bval"
    if lower in ("bvecs", "bvec"):
        return "bvec"
    return "dir" if is_dir else "any"


def _head(path, n=4096, gz=False):
    opener = gzip.open if gz else open
    with opener(path, "rb") as f:
        return f.read(n)


def _gunzip_all(path):
    """Read the whole stream so truncation and CRC errors surface."""
    with gzip.open(path, "rb") as f:
        while f.read(CHUNK):
            pass


def _nifti_shape(head):
    for endian in "<>":
        size = struct.unpack_from(endian + "i", head, 0)[0]
        if size == 348 and len(head) >= 348:
            dims = struct.unpack_from(endian + "8h", head, 40)
            return list(dims[1:1 + dims[0]]) if 0 < dims[0] <= 7 else None
        if size == 540 and len(head) >= 540:
            dims = struct.unpack_from(endian + "8q", head, 16)
            return list(dims[1:1 + dims[0]]) if 0 < dims[0] <= 7 else None
    return None


def _numbers(path):
    rows = [line.split() for line in pathlib.Path(path).read_text().splitlines() if line.strip()]
    return [[float(x) for x in row] for row in rows]


def check_file(path, fmt):
    """(errors, warnings, info) for one uploaded file or directory."""
    name = os.path.basename(path)
    errors, warnings, info = [], [], {}
    p = pathlib.Path(path)
    if fmt in ("dir", "zarr"):
        if not p.is_dir():
            return [f"{name}: expected a directory"], warnings, info
        entries = os.listdir(p)
        if not entries:
            warnings.append(f"{name}: directory is empty")
        if fmt == "zarr" and not {"zarr.json", ".zgroup", ".zarray"} & set(entries):
            warnings.append(f"{name}: no zarr.json, .zgroup or .zarray found")
        return errors, warnings, info
    if not p.is_file():
        return [f"{name}: expected a file"], warnings, info
    if p.stat().st_size == 0:
        return [f"{name}: file is empty"], warnings, info
    try:
        if fmt in ("nifti-gz", "mgz", "gzip"):
            _gunzip_all(path)
        if fmt in ("nifti", "nifti-gz"):
            shape = _nifti_shape(_head(path, 544, gz=fmt == "nifti-gz"))
            if shape is None:
                errors.append(f"{name}: not a NIfTI-1 or NIfTI-2 image")
            else:
                info["shape"] = shape
        elif fmt in ("mgh", "mgz"):
            if struct.unpack(">i", _head(path, 4, gz=fmt == "mgz"))[0] != 1:
                errors.append(f"{name}: not an MGH/MGZ image")
        elif fmt == "json":
            json.loads(p.read_text())
        elif fmt in ("tsv", "csv"):
            rows = list(csv.reader(p.read_text().splitlines(), delimiter="\t" if fmt == "tsv" else ","))
            if not rows or not any(rows[0]):
                warnings.append(f"{name}: no header row")
            elif any(len(r) != len(rows[0]) for r in rows[1:] if r):
                warnings.append(f"{name}: rows have different numbers of columns")
        elif fmt == "bval":
            values = [x for row in _numbers(path) for x in row]
            info["count"] = len(values)
        elif fmt == "bvec":
            rows = _numbers(path)
            if len(rows) != 3:
                warnings.append(f"{name}: expected 3 rows (FSL format), found {len(rows)}")
            info["count"] = len(rows[0]) if rows else 0
        elif fmt == "mat":
            head = _head(path, 1024)
            if not (head.startswith(b"MATLAB") or b"\x89HDF" in head[:1024]):
                errors.append(f"{name}: not a MATLAB (.mat/.set) file")
        elif fmt in MAGIC:
            if not any(_head(path, 32).startswith(m) for m in MAGIC[fmt]):
                errors.append(f"{name}: does not look like a {fmt} file")
        elif fmt in TEXT_MARKERS:
            if TEXT_MARKERS[fmt].lower() not in _head(path).lower():
                warnings.append(f"{name}: does not look like {fmt}")
    except (OSError, EOFError, ValueError, UnicodeDecodeError, struct.error, gzip.BadGzipFile, zlib.error) as e:
        errors.append(f"{name}: cannot be read as {fmt} ({e.__class__.__name__}: {e})")
    return errors, warnings, info


def _link(config_path, filename):
    os.makedirs("output", exist_ok=True)
    dest = os.path.join("output", filename)
    if os.path.lexists(dest):
        os.remove(dest)
    os.symlink(os.path.join("..", config_path), dest)


def _consistency(results):
    """Cross-file checks: gradient counts against each other and the DWI volume count."""
    warnings = []
    counts = {fid: r["count"] for fid, r in results.items() if "count" in r}
    if "bvals" in counts and "bvecs" in counts and counts["bvals"] != counts["bvecs"]:
        warnings.append(f"bvals has {counts['bvals']} entries but bvecs has {counts['bvecs']}")
    shape = results.get("dwi", {}).get("shape")
    if shape and len(shape) == 4 and "bvals" in counts and counts["bvals"] != shape[3]:
        warnings.append(f"bvals has {counts['bvals']} entries but dwi has {shape[3]} volumes")
    return warnings


def run(workdir, rules):
    workdir = pathlib.Path(workdir)
    os.chdir(workdir)
    config = json.loads(pathlib.Path("config.json").read_text())
    out = (config.get("_outputs") or [{}])[0]
    product = {"errors": [], "warnings": [], "meta": out.get("meta", {}), "tags": out.get("tags", []),
               "datatype_tags": out.get("datatype_tags", []), "brainlife": []}
    rule = rules.get(out.get("datatype", ""))
    uploaded = {k: v for k, v in config.items() if not k.startswith("_") and isinstance(v, str)}
    files = rule["files"] if rule else [{"id": k, "filename": os.path.basename(v), "required": False,
                                         "format": "any"} for k, v in uploaded.items()]
    if not rule:
        product["warnings"].append("datatype has no catalog rules; files were linked without checks")
    results = {}
    for f in files:
        target = f.get("filename") or f.get("dirname")
        if f["id"] not in uploaded:
            if f.get("required"):
                product["errors"].append(f"required file {f['id']} ({target}) was not uploaded")
            continue
        try:
            fmt = f.get("format") or format_for(target, "dirname" in f and "filename" not in f)
            errors, warnings, info = check_file(uploaded[f["id"]], fmt)
            product["errors"] += errors
            product["warnings"] += warnings
            results[f["id"]] = info
        except Exception as e:  # never let a validator bug block an upload
            product["warnings"].append(f"internal validator error on {f['id']}: {e!r}")
            traceback.print_exc()
        _link(uploaded[f["id"]], target)
    known = {f["id"] for f in files}
    for fid, path in uploaded.items():
        if fid not in known:  # e.g. a file added on brainlife.io after rules.json was generated
            product["warnings"].append(f"{fid} is not in the catalog rules for this datatype; linked unchecked")
            _link(path, os.path.basename(path))
    try:
        product["warnings"] += _consistency(results)
    except Exception as e:
        product["warnings"].append(f"internal validator error in cross-file checks: {e!r}")
    pathlib.Path("product.json").write_text(json.dumps(product, indent=1))
    return product


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rules", default=str(HERE / "rules.json"))
    a = p.parse_args(argv)
    product = run(os.getcwd(), json.loads(pathlib.Path(a.rules).read_text()))
    for e in product["errors"]:
        print("ERROR", e)
    for w in product["warnings"]:
        print("WARNING", w)


if __name__ == "__main__":
    sys.exit(main())
