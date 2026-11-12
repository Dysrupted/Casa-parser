# Mubawab fixtures

`*.synthetic.html` files (here and in `../avito`, `../sarouty`) are **hand-written** to mirror Mubawab's known card/detail
structure — the build container could not reach mubawab.ma. Replace them with recorded
pages before trusting the parser:

    radar record-fixture "<search url>" tests/fixtures/mubawab/search_p1.html
    radar record-fixture "<listing url>" tests/fixtures/mubawab/detail.html --kind detail

then update expected values in `tests/test_mubawab_parser.py` (and selectors in
`config/sources.yaml` if they no longer match).
