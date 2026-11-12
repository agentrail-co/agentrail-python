# Contributing to agentrail-python

Issues and pull requests are welcome. Never include real credentials,
identities or anything from a customer in an issue, a test or a commit: this
repository is public.

## Development

```sh
uv sync
uv run ruff format --check src tests integration scripts
uv run ruff check src tests integration scripts
uv run pyright
uv run pytest
scripts/check-consumer.sh                       # the built wheel, FastAPI and Flask, in a fresh venv
ARGUS_DIR=/path/to/argus integration/run.sh     # the real collector, with Docker
```

CI runs the first five on Python 3.10, 3.11, 3.12 and 3.13
(`.github/workflows/ci.yml`).

A test must earn its place: finish "this test fails if someone …" with a
realistic change, and watch it fail by making that change.

The receiver gate needs a checkout of Agentrail's collector with its
dependencies installed and Docker running; see
[integration/README.md](integration/README.md). Shared test vectors and their
provenance are in [testdata/](testdata/README.md). They are copied, never
edited here.

## Releasing

A `v<version>` tag publishes the package to PyPI
(`.github/workflows/release.yml`). The tag must match `VERSION` in
`src/agentrail_python/_version.py`.

1. Bump `VERSION`, run `uv lock`, and merge the change to `main`.
2. Tag `main` and push the tag:

   ```sh
   git tag -a v0.1.1 -m "agentrail-python v0.1.1"
   git push origin v0.1.1
   ```

3. The build job runs the checks, builds the wheel and sdist, and runs the
   consumer check. The publish job then waits for approval in the `pypi`
   environment: open the run under **Actions**, choose **Review deployments**,
   and approve.

Only the repository's admins can push a `v*` tag. PyPI trusts the workflow
(trusted publishing), so no token is stored, and each file gets a signed
attestation. A run that uploaded some files and then failed can be run again:
it skips what PyPI already has. PyPI never accepts a version twice; fix a bad
release with a new version and yank the old one.

Before 1.0, a minor version (0.1 to 0.2) may change what the package offers.
