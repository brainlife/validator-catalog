import gzip
import json
import os
import pathlib
import struct
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import validate  # noqa: E402


def nifti1(shape=(2, 2, 2, 3)):
    hdr = bytearray(352)
    struct.pack_into("<i", hdr, 0, 348)
    dims = [len(shape)] + list(shape) + [1] * (7 - len(shape))
    struct.pack_into("<8h", hdr, 40, *dims)
    hdr[344:348] = b"n+1\0"
    return bytes(hdr)


def write(tmp, name, data):
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return str(p)


# --- formats --------------------------------------------------------------

def test_format_from_file_name():
    assert validate.format_for("dwi.nii.gz", False) == "nifti-gz"
    assert validate.format_for("dwi.bvecs", False) == "bvec"
    assert validate.format_for("sub.ome.zarr", True) == "zarr"
    assert validate.format_for("rois", True) == "dir"
    assert validate.format_for("notes.xyz", False) == "any"


def test_valid_nifti_gz_reports_shape(tmp_path):
    path = write(tmp_path, "t1.nii.gz", gzip.compress(nifti1((4, 5, 6))))
    errors, warnings, info = validate.check_file(path, "nifti-gz")
    assert errors == [] and info["shape"] == [4, 5, 6]


def test_corrupt_gzip_is_an_error(tmp_path):
    good = gzip.compress(nifti1())
    path = write(tmp_path, "t1.nii.gz", good[:-12])
    errors, _, _ = validate.check_file(path, "nifti-gz")
    assert errors


def test_not_a_nifti_is_an_error(tmp_path):
    path = write(tmp_path, "t1.nii.gz", gzip.compress(b"hello" * 100))
    assert validate.check_file(path, "nifti-gz")[0]


def test_invalid_json_is_an_error(tmp_path):
    assert validate.check_file(write(tmp_path, "a.json", "{nope"), "json")[0]
    assert validate.check_file(write(tmp_path, "b.json", '{"a": 1}'), "json")[0] == []


def test_ragged_tsv_is_a_warning(tmp_path):
    errors, warnings, _ = validate.check_file(write(tmp_path, "c.tsv", "a\tb\n1\t2\n3\n"), "tsv")
    assert errors == [] and warnings


def test_bvecs_must_be_numeric_three_rows(tmp_path):
    assert validate.check_file(write(tmp_path, "dwi.bvecs", "0 1\n0 0\n0 0\n"), "bvec")[0] == []
    assert validate.check_file(write(tmp_path, "x.bvecs", "a b\n"), "bvec")[0]
    assert validate.check_file(write(tmp_path, "y.bvecs", "0 1\n0 0\n"), "bvec")[1]


def test_magic_bytes(tmp_path):
    assert validate.check_file(write(tmp_path, "a.png", b"\x89PNG\r\n\x1a\n...."), "png")[0] == []
    assert validate.check_file(write(tmp_path, "b.png", b"GIF89a"), "png")[0]
    assert validate.check_file(write(tmp_path, "c.fif", b"\x00\x00\x00\x64rest"), "fif")[0] == []
    assert validate.check_file(write(tmp_path, "d.edf", b"0       header"), "edf")[0] == []
    assert validate.check_file(write(tmp_path, "e.tck", b"mrtrix tracks\n"), "tck")[0] == []


def test_empty_directory_is_a_warning(tmp_path):
    (tmp_path / "rois").mkdir()
    errors, warnings, _ = validate.check_file(str(tmp_path / "rois"), "dir")
    assert errors == [] and warnings


# --- whole run ------------------------------------------------------------

RULES = {"58c33c5fe13a50849b25879b": {"name": "neuro/dwi", "files": [
    {"id": "dwi", "filename": "dwi.nii.gz", "required": True, "format": "nifti-gz"},
    {"id": "bvecs", "filename": "dwi.bvecs", "required": True, "format": "bvec"},
    {"id": "bvals", "filename": "dwi.bvals", "required": True, "format": "bval"},
    {"id": "sbref", "filename": "sbref.nii.gz", "required": False, "format": "nifti-gz"}]}}


def _upload(tmp, bvals="0 1000 1000"):
    up = tmp / "task" / "upload"
    write(up, "dwi.nii.gz", gzip.compress(nifti1((2, 2, 2, 3))))
    write(up, "dwi.bvecs", "0 1 0\n0 0 1\n0 0 0\n")
    write(up, "dwi.bvals", bvals)
    work = tmp / "validator"
    work.mkdir()
    config = {"dwi": "../task/upload/dwi.nii.gz", "bvecs": "../task/upload/dwi.bvecs",
              "bvals": "../task/upload/dwi.bvals",
              "_outputs": [{"id": "upload", "datatype": "58c33c5fe13a50849b25879b", "meta": {"subject": "01"},
                            "tags": ["t"], "datatype_tags": ["single_shell"]}]}
    (work / "config.json").write_text(json.dumps(config))
    return work


def test_valid_upload_links_files_and_passes_metadata(tmp_path):
    work = _upload(tmp_path)
    product = validate.run(work, RULES)
    assert product["errors"] == []
    assert product["meta"] == {"subject": "01"} and product["datatype_tags"] == ["single_shell"]
    assert os.path.islink(work / "output" / "dwi.nii.gz")
    assert (work / "output" / "dwi.bvals").read_text() == "0 1000 1000"
    assert json.loads((work / "product.json").read_text()) == product


def test_gradient_count_mismatch_is_a_warning(tmp_path):
    product = validate.run(_upload(tmp_path, bvals="0 1000"), RULES)
    assert product["errors"] == [] and any("bvals" in w for w in product["warnings"])


def test_missing_required_file_is_an_error(tmp_path):
    work = _upload(tmp_path)
    config = json.loads((work / "config.json").read_text())
    del config["bvecs"]
    (work / "config.json").write_text(json.dumps(config))
    assert any("bvecs" in e for e in validate.run(work, RULES)["errors"])


def test_unknown_datatype_only_links_files(tmp_path):
    work = _upload(tmp_path)
    product = validate.run(work, {})
    assert product["errors"] == [] and product["warnings"]
    assert os.path.islink(work / "output" / "dwi.nii.gz")


def test_internal_failure_never_blocks_an_upload(tmp_path, monkeypatch):
    work = _upload(tmp_path)
    monkeypatch.setattr(validate, "check_file", lambda *a: 1 / 0)
    product = validate.run(work, RULES)
    assert product["errors"] == [] and any("internal" in w for w in product["warnings"])
    assert os.path.islink(work / "output" / "dwi.nii.gz")


def test_command_line_runs_in_the_task_directory(tmp_path):
    work = _upload(tmp_path)
    (work / "rules.json").write_text(json.dumps(RULES))
    subprocess.run([sys.executable, str(ROOT / "validate.py"), "--rules", str(work / "rules.json")],
                   cwd=work, check=True)
    assert json.loads((work / "product.json").read_text())["errors"] == []


def test_garbled_gzip_body_is_an_error(tmp_path):
    good = bytearray(gzip.compress(nifti1() * 20))
    for i in range(20, 60):
        good[i] ^= 0xFF
    errors, _, _ = validate.check_file(write(tmp_path, "t1.nii.gz", bytes(good)), "nifti-gz")
    assert errors


def test_format_comes_from_the_file_name_when_rules_omit_it(tmp_path):
    rules = {k: {"name": v["name"], "files": [{x: y for x, y in f.items() if x != "format"} for f in v["files"]]}
             for k, v in RULES.items()}
    work = _upload(tmp_path)
    (tmp_path / "task" / "upload" / "dwi.bvals").write_text("not numbers")
    assert any("dwi.bvals" in e for e in validate.run(work, rules)["errors"])
