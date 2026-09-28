"""Unit tests: corpus specification validation failure categories."""

from __future__ import annotations

import pytest

from wfst_service.corpus.errors import SpecError
from wfst_service.corpus.spec import build_corpus, parse_corpus


def _td_body(**overrides) -> dict:
    body = {
        "name": "t",
        "kind": "fst",
        "start": 0,
        "finals": [{"state": 0, "cost": 0.0}],
        "arcs": [{"src": 0, "dst": 0, "in": "a", "out": "a", "cost": 0.0}],
    }
    body.update(overrides)
    return body


@pytest.mark.unit
@pytest.mark.parametrize(
    "document,path_fragment",
    [
        ({"version": 1, "transducers": []}, "transducers"),
        (
            {"version": 1, "transducers": [_td_body(name="")]},
            "name",
        ),
        (
            {
                "version": 1,
                "transducers": [_td_body(name="t"), _td_body(name="t")],
            },
            "name",
        ),
        (
            {"version": 1, "transducers": [_td_body(kind="nope")]},
            "kind",
        ),
        (
            {"version": 1, "transducers": [_td_body(start=-1)]},
            "start",
        ),
        (
            {"version": 1, "transducers": [_td_body(finals=[])]},
            "finals",
        ),
        (
            {
                "version": 1,
                "transducers": [
                    _td_body(
                        arcs=[
                            {"src": 0, "dst": 0, "in": "ab", "out": "a"}
                        ]
                    )
                ],
            },
            "in",
        ),
        (
            {
                "version": 1,
                "transducers": [
                    _td_body(
                        arcs=[
                            {"src": 0, "dst": 0, "in": "a", "out": "a",
                             "cost": "cheap"}
                        ]
                    )
                ],
            },
            "cost",
        ),
        (
            {
                "version": 1,
                "transducers": [_td_body()],
                "pipelines": [{"name": "p", "sequence": ["t", "ghost"]}],
            },
            "sequence",
        ),
        ({"version": 99, "transducers": [_td_body()]}, "version"),
    ],
)
def test_invalid_documents_raise_spec_error_with_path(
    document: dict, path_fragment: str
) -> None:
    with pytest.raises(SpecError) as excinfo:
        parse_corpus(document)
    assert excinfo.value.code == "spec_error"
    assert path_fragment in excinfo.value.path


@pytest.mark.unit
def test_invalid_json_file_reports_position(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")
    from wfst_service.corpus.spec import load_corpus_document

    with pytest.raises(SpecError) as excinfo:
        load_corpus_document(path)
    assert "not valid JSON" in str(excinfo.value)


@pytest.mark.unit
def test_lexicon_entries_expand_to_character_level_arcs() -> None:
    doc = {
        "version": 1,
        "transducers": [
            {
                "name": "lex",
                "kind": "lexicon",
                "identity_alphabet": "ab",
                "entries": [
                    {"input": "ab", "outputs": [["ba", 0.5], ["ab", 0.0]]}
                ],
            }
        ],
    }
    built = build_corpus(parse_corpus(doc))
    fst = built.fsts["lex"]
    # Every arc label is a single character or epsilon.
    for arc in fst.arcs:
        assert len(arc.ilabel) <= 1
        assert len(arc.olabel) <= 1
    # The entry emits two distinct outputs, hence a branching terminal.
    emission = [a for a in fst.arcs if a.ilabel == "" and a.olabel != ""]
    outputs = "".join(sorted({a.olabel for a in emission}))
    assert outputs == "ab"


@pytest.mark.unit
def test_lexicon_requires_non_empty_outputs_list() -> None:
    with pytest.raises(SpecError):
        parse_corpus(
            {
                "version": 1,
                "transducers": [
                    {
                        "name": "lex",
                        "kind": "lexicon",
                        "entries": [{"input": "a", "outputs": []}],
                    }
                ],
            }
        )
