# Text iterator and virtual-column scope validation

An independently invented Import model contains integer Sales amounts, regions and years, with four original helpers/roots: Revenue, Delta, Ready and Details. Details builds a stored `ADDCOLUMNS` table, filters negative deltas and renders sorted text with `CONCATENATEX`, `&`, `FORMAT` and `UNICHAR`. Ready projects a virtual Year column with the same name as a physical model column. No external report formulas or data are included.

The generated Details candidate carries actual `SUMX(Sales,Sales[Amount])` → `SUM(Sales[Amount])` changes from its helper path into the new root, preserving explicit `CALCULATE` transitions and the unchanged original helpers. Text, delimiters and sort expressions are retained. Tests in `tests/test_text_iterators.py` construct this fixture.

Native Fabric evaluation on 2026-10-22 passed 54 region/year filter combinations. Each context checked strict original/candidate text equality and BLANK identity. Independent Python source-row calculations checked both versions' expected output, arithmetic/concatenation precedence, BLANK formatting, ascending and descending virtual-year ordering, and multiple sort keys. This yielded 378 oracle assertions. The actual exported DEFINE/EVALUATE preview query also executed successfully. The temporary model was deleted and its absence verified.

No CPU, latency or capacity savings were measured in this test. These are finite correctness checks for a supported subset, not an implementation of the DAX engine or proof about arbitrary models. Unsupported syntax, unresolved helpers, true measure/column ambiguities and out-of-scope aliases continue to withhold automatic rewrites. Formatting is delegated to the native engine; the analyzer never evaluates locale rules itself.

The deployment harness initially omitted the V3 model-source declaration and the service rejected the import. After adding `defaultPowerBIDataSourceVersion: powerBI_V3` to the temporary harness model, import and refresh succeeded. This was a harness repair, not a changed assertion or candidate.

Semantics references: [DAX operator precedence](https://learn.microsoft.com/en-us/dax/dax-operator-reference), [CONCATENATEX](https://learn.microsoft.com/en-us/dax/concatenatex-function-dax), [FORMAT](https://learn.microsoft.com/en-us/dax/format-function-dax).
