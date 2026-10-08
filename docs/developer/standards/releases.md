# Release Standards

Publish each version once from one selected source commit, validate the built distributions, and publish those same bytes. The published package name/version is the canonical production producer identity; the source commit is provenance. Version and contract classification belong to [Versioning](../versioning.md); this page owns the distribution and publication workflow.

## Select the release source

Land the version, changelog, and affected contract documentation on `main` through the normal reviewed workflow. Create an annotated `v<project-version>` tag on the intended commit and publish its immutable GitHub release.

The [publication workflow](../../../.github/workflows/publish.yml) checks out that revision and provisions the locked development environment through the [shared setup action](../../../.github/actions/setup-python-uv/action.yml). Before quality checks or distribution builds, it queries the triggering release ID through the REST API. The gate requires the expected ID and tag, published state, explicit boolean immutability, and agreement with the checked-out project's version. The repository setting applies only to future releases; neither it nor the webhook proves this release's immutability or version agreement. GitHub locks the published tag and prevents reuse of its name, including after release deletion.

Different code requires a new version. Publication runs for a tag are serialized and PyPI duplicate-file rejection is not skipped. GitHub immutability protects its tag and assets, not a separate package index; the fixed wheel/source filenames and failing duplicate upload preserve the once-only PyPI publication path. A partially failed upload requires release-process review, not a rebuild, silently skipped files, or another producer under that version.

The checked-out commit is the release source. Neither uncommitted files nor a later branch tip belongs in that release. Provisioning may access package or advisory services; it is separate from financial processing.

## Build and inspect ordinary distributions

Build from the clean release-tag checkout with ordinary `uv build --no-sources`. The existing `uv_build` backend owns packaging. No custom Git archive, staged source tree, producer stamp, or build identifier is needed. The immutable tag and release workflow establish the release-to-source link outside the installed package.

The [release checker](../../../.github/scripts/check_release.py) reads standard wheel `METADATA` and source-distribution `PKG-INFO`. It requires exactly one of each artifact, one unambiguous metadata record identifying the checked-out project name/version, matching declared Python/dependency constraints, matching project declarations in the source archive, and matching inventories and bytes for the complete `jbt` package, including contract resources. Backend normalization of TOML formatting is allowed. Duplicate archive entries and non-regular source payloads fail. It neither installs nor imports the built package.

For a local release rehearsal, check out the intended source and use an empty output directory:

```bash
uv build --no-sources --out-dir dist
uv run --locked python .github/scripts/check_release.py --distributions dist
set -- dist/*.whl
test "$#" -eq 1
wheel="$1"
uv run --locked pytest -q -m e2e --release-wheel "$wheel"
```

Ordinary packaging uses the current checkout, including uncommitted edits. A local rehearsal tests those bytes; it does not publish or authenticate a release. Production publication builds only the immutable release-tag checkout. The end-to-end suite owns private temporary tool environments, so no separate installation or replacement of a user's existing tool is required.

## Validate and publish the same artifacts

The release build job runs Ruff, type checking, test-quality checks, and the ordinary pytest suite before building one wheel/source pair. It inspects those archives and runs `uv run --locked pytest -q -m e2e --release-wheel "$wheel"` against the final wheel without rebuilding or installing it solely for metadata checks.

Installed acceptance verifies independent financial results and uncached recovery after deleting and reinstalling the tool environment. Archive inspection establishes package consistency, not an artifact signature or exact dependency reconstruction.

After validation, the build job uploads `dist/`. The dependent publication job downloads that artifact and publishes it to PyPI without rebuilding. It uses the protected PyPI publication environment and narrowly scoped identity-token permission for trusted publishing; build and checkout need only repository read access. Do not regenerate distributions between validation and publication.

## Release availability

Preserve published releases where practical: historical verification depends on obtaining the recorded version in a supported environment. Report unavailable or unsupported releases honestly. Exact historical dependencies or runnable archives require a separately justified design, not an implicit extension of publication.
