# validator-catalog

One brainlife.io validator for every datatype that has no dedicated one.

When someone uploads data to brainlife.io (web upload or `bl data upload`), brainlife runs the
datatype's *validator* before the data is archived. A validator checks the uploaded files, links
them into `output/` for the archiver, and reports errors (which stop the upload) and warnings
(which are shown but let it through). Until now 116 of 140 datatypes had no validator, so their
uploads were archived unchecked and could not be made through the web UI.

This App validates any datatype from its file list in the
[datatype catalog](https://github.com/brainlife/abcd-spec/tree/master/datatypes). Datatypes that
already have a dedicated validator (for example `brainlife/validator-neuro-anat`) keep it.

## What it checks

| Check | Result if it fails |
|---|---|
| A required file or directory was not uploaded | error |
| A file is empty | error |
| A gzip file is truncated or corrupt (`.nii.gz`, `.mgz`, `.gz`) | error |
| A NIfTI file has no valid NIfTI-1 or NIfTI-2 header | error |
| A JSON file does not parse | error |
| bvals/bvecs contain something other than numbers | error |
| A file's leading bytes do not match its format: MGH/MGZ, PNG, JPEG, PDF, HDF5, ZIP/TRX, MAT/EEGLAB, EDF, BDF, FIF, BrainVision header, MRtrix `.tck`, TrackVis `.trk` | error |
| A TSV/CSV has no header row or uneven columns | warning |
| bvecs is not 3 rows; bvals and bvecs counts differ; bvals differ from the DWI volume count | warning |
| SVG, HTML or GIfTI text markers are missing | warning |
| A directory is empty; a Zarr directory has no `zarr.json`, `.zgroup` or `.zarray` | warning |

The format of each file comes from its name in the catalog (`dwi.nii.gz` is NIfTI, `channels.tsv`
is TSV, and so on). Files with unknown extensions are only checked for being present and not empty.

**It never blocks an upload because of its own bug.** Any unexpected exception is reported as a
warning and the files are still linked into `output/`.

Metadata, tags and datatype tags given at upload are passed through unchanged.

## How it runs on brainlife.io

- `main` runs `validate.py` in `docker://python:3.12-slim`. It uses only the Python standard
  library.
- `config.json` (written by brainlife's `finalize-upload`) maps each file id to its uploaded path;
  `_outputs[0].datatype` says which datatype was uploaded.
- `rules.json` lists every datatype's files (`id`, `filename` or `dirname`, `required`). It is
  generated from the catalog, never edited by hand:

  ```
  cd abcd-spec
  python tools/bldt.py validator-rules ../validator-catalog/rules.json
  ```

- A datatype uses this validator when its record on brainlife.io has
  `validator: brainlife/validator-catalog` and `validator_branch: main`. The catalog tool attaches
  it only to datatypes with no validator, and writes an undo log:

  ```
  python tools/bldt.py set-validator --dry-run          # list what would change
  python tools/bldt.py set-validator --only neuro/pet   # one datatype
  python tools/bldt.py undo docs/backfill/datatypes/<stamp>-validator-undo.json
  ```

## Run it locally

```
cd <a directory with config.json and the uploaded files>
python /path/to/validator-catalog/validate.py
cat product.json
```

## Tests

```
pip install pytest
pytest -q tests
```

## Design

[`docs/superpowers/specs/2026-11-25-catalog-validator-design.md`](https://github.com/brainlife/abcd-spec/blob/master/docs/superpowers/specs/2026-11-25-catalog-validator-design.md)
in abcd-spec.
