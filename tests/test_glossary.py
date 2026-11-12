# -*- coding: utf-8 -*-
"""术语表回归：规范写法、别名、全大写模糊纠错、缓存失效、检索扩展。"""
from __future__ import annotations

import json

import pytest
from helpers import add_rows, add_session, utt_row

from app import glossary, profile, store


SID = "S_GLOSS"


def _terms(*items):
    return glossary.save([{"term": t, "aliases": list(a)} for t, a in items])


def test_canonical_ascii_spacing_and_case(isolated_paths):
    _terms(("FDE", []), ("OPC", []))
    assert glossary.correct("F D E") == "FDE"
    assert glossary.correct("F-D-E") == "FDE"
    assert glossary.correct("F.D.E") == "FDE"
    assert glossary.correct("fde") == "FDE"
    assert glossary.correct("今天讲 F D E 和 opc") == "今天讲 FDE 和 OPC"


def test_ascii_pattern_does_not_eat_inside_words(isolated_paths):
    _terms(("OPC", []))
    assert glossary.correct("XOPCQ") == "XOPCQ"
    assert glossary.correct("OPC12") == "OPC12"     # 编辑距离 >1，不猜
    assert glossary.correct("(OPC)") == "(OPC)"


def test_alias_replacement(isolated_paths):
    _terms(("OPC", ["欧匹西", "O P C"]))
    assert glossary.correct("昨天聊了欧匹西政策") == "昨天聊了OPC政策"
    assert glossary.correct("O P C 的报价") == "OPC 的报价"


def test_longest_alias_wins(isolated_paths):
    _terms(("OPC", ["欧匹西"]), ("OPC政策", ["欧匹西政策"]))
    assert glossary.correct("欧匹西政策") == "OPC政策"


def test_fuzzy_only_for_uppercase_tokens(isolated_paths):
    _terms(("FDE", []))
    assert glossary.correct("FDI") == "FDE"          # 编辑距离 1
    assert glossary.correct("FDI 和 xyz") == "FDE 和 xyz"
    assert glossary.correct("the") == "the"          # 小写普通词不动
    assert glossary.correct("ABCD") == "ABCD"        # 差太远不动
    assert glossary.correct("FDE") == "FDE"          # 已经正确


def test_correct_is_idempotent(isolated_paths):
    _terms(("FDE", ["艾弗迪"]), ("OPC", ["欧匹西"]))
    once = glossary.correct("艾弗迪 的 F D E 和欧匹西")
    assert once == "FDE 的 FDE 和OPC"
    assert glossary.correct(once) == once


def test_save_dedupes_case_insensitively_and_drops_self_alias(isolated_paths):
    out = glossary.save([
        {"term": "OPC", "aliases": ["欧匹西", "opc", ""]},
        {"term": "opc", "aliases": ["x"]},
        {"term": "  ", "aliases": []},
    ])
    assert [e["term"] for e in out] == ["OPC"]
    assert out[0]["aliases"] == ["欧匹西"]


def test_profile_keywords_join_glossary_once(isolated_paths):
    _terms(("OPC", []))
    profile.save(["OPC", "热点"])
    terms = glossary.terms()
    assert terms.count("OPC") == 1
    assert "热点" in terms
    e = next(x for x in glossary.entries() if x["term"] == "热点")
    assert e["note"] == "关注词"


def test_missing_and_malformed_glossary_falls_back(isolated_paths):
    assert glossary.entries() == []
    assert glossary.correct("原样返回") == "原样返回"
    isolated_paths.joinpath("glossary.json").write_text("{not json", encoding="utf-8")
    glossary._cache["key"] = None
    glossary._rules_cache["key"] = None
    assert glossary.entries() == []
    assert glossary.correct("F D E") == "F D E"


def test_corrupt_entry_shapes_are_skipped(isolated_paths):
    isolated_paths.joinpath("glossary.json").write_text(json.dumps({
        "terms": ["OPC", {"term": ""}, {"term": "FDE", "aliases": "不是列表"},
                  42, {"term": "  "}]
    }, ensure_ascii=False), encoding="utf-8")
    glossary._cache["key"] = None
    glossary._rules_cache["key"] = None
    assert glossary.terms() == ["OPC", "FDE"]


def test_save_invalidates_correct_cache_even_same_second(isolated_paths):
    _terms(("FDE", []))
    assert glossary.correct("F D E") == "FDE"
    # 同一秒内改成不含 FDE 的表；mtime 可能不变，但 save() 必须主动废掉缓存
    _terms(("OPC", []))
    assert glossary.correct("F D E") == "F D E"


def test_expand_query_splits_and_adds_aliases(isolated_paths):
    _terms(("OPC", ["欧匹西"]))
    assert glossary.expand_query("OPC政策") == ["OPC政策", "OPC", "政策"]
    q = glossary.expand_query("欧匹西")
    assert q[0] == "欧匹西" and "OPC" in q
    assert glossary.expand_query("") == []


def test_reapply_skips_edited_rows_and_clears_ai_cache(db):
    add_session(db, SID)
    ids = add_rows(db, SID, [
        utt_row(0, "说的欧匹西", t=0),
        utt_row(1, "人工改过的欧匹西", t=10),
    ])
    store.update_utterance(db, SID, ids[1], text="人工改过的欧匹西")
    db.execute("UPDATE utterances SET ai_fp='fp', features_json='{}' WHERE id=?", (ids[0],))
    db.commit()
    _terms(("OPC", ["欧匹西"]))
    out = glossary.reapply(db, SID)
    assert out["scanned"] == 1 and out["changed"] == 1
    rows = {r["id"]: dict(r) for r in db.execute("SELECT * FROM utterances")}
    assert rows[ids[0]]["text"] == "说的OPC"
    assert rows[ids[0]]["ai_fp"] is None and rows[ids[0]]["features_json"] is None
    assert rows[ids[1]]["text"] == "人工改过的欧匹西"    # 人工校对优先
