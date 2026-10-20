# Contributing

Use Python 3.10 or later. Create a virtual environment and install the project in editable mode:

```shell
python -m pip install -e .
python -m unittest discover -s tests -v
```

Build and check the distributions with:

```shell
python -m pip install build
python tools/build_release.py
```

The build script normalizes source-archive ownership metadata and checks the wheel and source archive for accidentally bundled local files, personal home paths and common credential formats. The GitHub Actions matrix tests Windows and Linux on Python 3.10–3.14.

Use small, fictional fixtures for bug reports and tests. Keep exported business models, telemetry, generated reports, source data and credentials outside the repository. Reports can include DAX expressions and source paths. Do not attach them to a public issue without first replacing sensitive content.

Changes to parsing should preserve conservative dependency handling: unresolved or ambiguous inputs must stay visible. Performance suggestions need correctness checks, and static review scores must not be presented as measured execution cost. Engine integrations should keep missing observations unknown and distinguish synthetic fixtures from real engine runs.

Submit focused pull requests explaining the behavior change and the relevant checks. Contributions are licensed under the project's MIT license.
