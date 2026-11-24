"""End-to-end tests through the Flask app, SQLite and the public API."""

from __future__ import annotations

import json

import pytest

from app import db, model

from .conftest import (
    active_version,
    api_station,
    publish_doc,
    station_doc,
    two_entrance_doc,
    with_outages,
)


def test_upload_rejects_invalid_json(client):
    resp = client.post(
        "/admin/upload", data={"document": "{not json"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert "JSON 解析失败".encode() in resp.data


def test_upload_rejects_unknown_node(client):
    doc = station_doc(passages=[{"from": "entrance:A", "to": "platform:P9"}])
    resp = client.post(
        "/admin/upload",
        data={"document": json.dumps(doc)},
        follow_redirects=True,
    )
    assert "校验失败".encode() in resp.data


def test_upload_then_publish_then_api(app, client):
    doc = two_entrance_doc()
    resp = client.post(
        "/admin/upload",
        data={"document": json.dumps(doc)},
        follow_redirects=True,
    )
    assert "草稿".encode() in resp.data

    resp = client.post("/admin/publish", data={"note": "v1"}, follow_redirects=True)
    assert "已发布".encode() in resp.data
    version = active_version(app)
    assert version is not None

    body = api_station(app, "T1").get_json()
    assert body["version_id"] == version
    assert body["station_id"] == "T1"
    platform = body["platforms"][0]
    assert platform["status"] == "ok"
    # Either entrance alone is enough: two alternative single-lift sets.
    assert platform["entrance_to_platform"]["minimal_elevator_sets"] == [
        ["E1"], ["E2"]
    ]
    assert platform["enterable"] is True
    assert platform["exitable"] is True


def test_api_404_before_publish(app):
    resp = api_station(app, "T1")
    assert resp.status_code == 503
    assert "error" in resp.get_json()


def test_api_404_for_unknown_station(app):
    publish_doc(app, two_entrance_doc())
    resp = api_station(app, "NOPE")
    assert resp.status_code == 404


def test_outage_changes_judgement_without_republish(app):
    publish_doc(app, two_entrance_doc())
    before = active_version(app)

    # One lift out leaves the other entrance route working.
    with_outages(app, ["E1"])
    body = api_station(app, "T1").get_json()
    platform = body["platforms"][0]
    assert platform["status"] == "ok"
    assert platform["entrance_to_platform"]["minimal_elevator_sets"] == [["E2"]]
    assert platform["entrance_to_platform"]["minimal_recovery_sets"] is None
    assert body["outages_applied"] == ["E1"]

    # Every route blocked: the platform becomes blocked and each direction
    # offers both lifts as alternative minimal recovery sets.
    with_outages(app, ["E1", "E2"])
    body = api_station(app, "T1").get_json()
    platform = body["platforms"][0]
    assert platform["status"] == "blocked"
    assert platform["entrance_to_platform"]["minimal_elevator_sets"] == []
    assert platform["entrance_to_platform"]["minimal_recovery_sets"] == [
        ["E1"], ["E2"]
    ]
    assert body["outages_applied"] == ["E1", "E2"]

    # Still the same published version: outages never require a republish.
    assert active_version(app) == before


def test_outage_editing_via_web_form(app):
    publish_doc(app, two_entrance_doc())
    client = app.test_client()
    client.post(
        "/admin/outages",
        data={"action": "add", "elevator": "E2", "station_id": "T1", "note": "检修"},
        follow_redirects=True,
    )
    body = api_station(app, "T1").get_json()
    assert body["outages_applied"] == ["E2"]
    # The E1 route still carries the platform.
    assert body["platforms"][0]["status"] == "ok"

    client.post(
        "/admin/outages",
        data={"action": "add", "elevator": "E1", "station_id": "T1"},
        follow_redirects=True,
    )
    body = api_station(app, "T1").get_json()
    assert body["platforms"][0]["status"] == "blocked"

    client.post(
        "/admin/outages",
        data={"action": "remove", "elevator": "E2"},
        follow_redirects=True,
    )
    body = api_station(app, "T1").get_json()
    assert body["platforms"][0]["status"] == "ok"


def test_bulk_outage_replace(client):
    publish_doc(client.application, two_entrance_doc())
    client.post(
        "/admin/outages",
        data={"action": "replace", "elevators": "E1\nE2"},
        follow_redirects=True,
    )
    body = client.get("/api/v1/stations/T1").get_json()
    assert body["outages_applied"] == ["E1", "E2"]


def test_station_list_endpoint(app):
    publish_doc(app, two_entrance_doc())
    resp = app.test_client().get("/api/v1/stations")
    assert resp.status_code == 200
    data = resp.get_json()
    assert [s["station_id"] for s in data["stations"]] == ["T1"]
    assert data["stations"][0]["status_summary"]["ok"] == 1


def test_publish_merges_old_and_new_station_drawings(app):
    publish_doc(app, two_entrance_doc())
    first_version = active_version(app)

    other = station_doc(station_id="T2", entrances=("X",), platforms=("Q1",))
    other["passages"] = [
        {"id": "p", "from": "entrance:X", "to": "platform:Q1"},
        {"id": "q", "from": "platform:Q1", "to": "entrance:X"},
    ]
    publish_doc(app, other)
    second_version = active_version(app)
    assert second_version != first_version

    # Old station still served, with its original tables.
    body = api_station(app, "T1").get_json()
    assert body["station_id"] == "T1"
    # New station served too.
    body = api_station(app, "T2").get_json()
    assert body["platforms"][0]["status"] == "no_elevator"


def test_publish_blocks_when_outaged_elevator_missing_from_new_map(app):
    publish_doc(app, two_entrance_doc())
    first_version = active_version(app)
    with_outages(app, ["E1"])
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            db.set_outage(conn, "E1", True, "T1")
            conn.commit()
        finally:
            conn.close()

    # New drawing drops E1 entirely.
    reduced = station_doc(
        passages=[
            {"from": "entrance:A", "to": "platform:P1"},
            {"from": "platform:P1", "to": "entrance:A"},
            {"from": "entrance:B", "to": "platform:P1", "elevator": "E2"},
            {"from": "platform:P1", "to": "entrance:B", "elevator": "E2"},
        ]
    )
    client = app.test_client()
    client.post(
        "/admin/upload", data={"document": json.dumps(reduced)}, follow_redirects=True
    )
    resp = client.post("/admin/publish", data={"note": "bad"}, follow_redirects=True)
    assert "缺少该电梯".encode() in resp.data
    assert active_version(app) == first_version

    # Old version + current outage list keep serving; the E2 route still
    # works, so the platform stays usable under the existential rule.
    body = api_station(app, "T1").get_json()
    assert body["version_id"] == first_version
    assert body["platforms"][0]["status"] == "ok"
    assert body["platforms"][0]["entrance_to_platform"][
        "minimal_elevator_sets"
    ] == [["E2"]]

    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            log = db.list_publish_log(conn)
        finally:
            conn.close()
    assert log[0]["status"] == "failed"


def test_publish_failure_keeps_old_version_when_calibration_fails(app, monkeypatch):
    publish_doc(app, two_entrance_doc())
    first_version = active_version(app)

    other = station_doc(station_id="T3", entrances=("X",), platforms=("Q1",))
    other["passages"] = [{"id": "p", "from": "entrance:X", "to": "platform:Q1"}]
    client = app.test_client()
    client.post(
        "/admin/upload", data={"document": json.dumps(other)}, follow_redirects=True
    )

    def boom(_doc):
        raise model.SchemaError("simulated calibration failure")

    monkeypatch.setattr(model, "parse_station", boom)
    resp = client.post("/admin/publish", data={}, follow_redirects=True)
    assert "核算失败".encode() in resp.data
    assert active_version(app) == first_version
    assert api_station(app, "T3").status_code == 404
    assert api_station(app, "T1").status_code == 200


def test_publish_aborts_if_drafts_change_during_calibration(app, monkeypatch):
    publish_doc(app, two_entrance_doc())
    first_version = active_version(app)

    extra = station_doc(station_id="T4", entrances=("X",), platforms=("Q1",))
    extra["passages"] = [{"id": "p", "from": "entrance:X", "to": "platform:Q1"}]
    client = app.test_client()
    client.post(
        "/admin/upload", data={"document": json.dumps(extra)}, follow_redirects=True
    )

    original = model.build_station_tables

    def tamper(station):
        # Simulate a concurrent upload while calibration is running.
        conn = db.connect(app.config["DATABASE"])
        try:
            doc = station_doc(station_id="T5", entrances=("Y",), platforms=("Q9",))
            doc["passages"] = [
                {"id": "p", "from": "entrance:Y", "to": "platform:Q9"}
            ]
            with conn:
                db.save_draft(conn, "T5", doc)
        finally:
            conn.close()
        return original(station)

    monkeypatch.setattr(model, "build_station_tables", tamper)
    resp = client.post("/admin/publish", data={}, follow_redirects=True)
    assert "核算期间草稿被修改".encode() in resp.data
    assert active_version(app) == first_version


def test_publish_uses_latest_outage_list(app, monkeypatch):
    """Outage edits made during calibration must be honoured at commit."""

    publish_doc(app, two_entrance_doc())
    first_version = active_version(app)

    changed = two_entrance_doc()
    changed["passages"] = [
        p for p in changed["passages"] if p["elevator"] != "E2"
    ]
    changed["passages"].append(
        {"id": "x", "from": "entrance:B", "to": "platform:P1"}
    )
    changed["passages"].append(
        {"id": "y", "from": "platform:P1", "to": "entrance:B"}
    )
    client = app.test_client()
    client.post(
        "/admin/upload", data={"document": json.dumps(changed)}, follow_redirects=True
    )

    original = model.build_station_tables

    def add_outage(station):
        conn = db.connect(app.config["DATABASE"])
        try:
            with conn:
                db.set_outage(conn, "E2", True, "T1")
        finally:
            conn.close()
        return original(station)

    monkeypatch.setattr(model, "build_station_tables", add_outage)
    resp = client.post("/admin/publish", data={}, follow_redirects=True)
    # E2 went out during calibration and the new drawing no longer has it:
    # the publish must abort because the latest list still names E2.
    assert "缺少该电梯".encode() in resp.data
    assert active_version(app) == first_version


def test_publish_succeeds_when_outage_added_during_calibration_is_present(app, monkeypatch):
    publish_doc(app, two_entrance_doc())

    client = app.test_client()
    original = model.build_station_tables

    def add_outage(station):
        conn = db.connect(app.config["DATABASE"])
        try:
            with conn:
                db.set_outage(conn, "E1", True, "T1")
        finally:
            conn.close()
        return original(station)

    # Re-upload the same drawing so there is a draft to publish.
    client.post(
        "/admin/upload",
        data={"document": json.dumps(two_entrance_doc())},
        follow_redirects=True,
    )
    monkeypatch.setattr(model, "build_station_tables", add_outage)
    resp = client.post("/admin/publish", data={"note": "ok"}, follow_redirects=True)
    assert "已发布".encode() in resp.data

    body = api_station(app, "T1").get_json()
    assert body["outages_applied"] == ["E1"]
    # The E2 entrance route still works, so the platform is not blocked.
    assert body["platforms"][0]["status"] == "ok"
    assert body["platforms"][0]["entrance_to_platform"][
        "minimal_elevator_sets"
    ] == [["E2"]]


def test_station_page_and_index_render(app):
    publish_doc(app, two_entrance_doc())
    client = app.test_client()
    assert "T1".encode() in client.get("/").data
    resp = client.get("/stations/T1")
    assert resp.status_code == 200
    assert "最小用梯组合".encode() in resp.data
    assert client.get("/stations/MISSING").status_code == 404
    assert client.get("/admin/upload").status_code == 200
    assert client.get("/admin/publish").status_code == 200
    assert client.get("/admin/outages").status_code == 200


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"ok": True}


def test_delete_draft(client):
    doc = two_entrance_doc()
    client.post(
        "/admin/upload", data={"document": json.dumps(doc)}, follow_redirects=True
    )
    client.post("/admin/drafts/delete", data={"station_id": "T1"}, follow_redirects=True)
    resp = client.post("/admin/publish", data={}, follow_redirects=True)
    assert "没有草稿".encode() in resp.data


def test_no_elevator_status_presented_separately(app):
    doc = station_doc(
        entrances=("A",),
        passages=[
            {"from": "entrance:A", "to": "platform:P1"},
            {"from": "platform:P1", "to": "entrance:A"},
        ],
    )
    publish_doc(app, doc)
    body = api_station(app, "T1").get_json()
    assert body["platforms"][0]["status"] == "no_elevator"
    assert body["platforms"][0]["enterable"] is True


def test_unreachable_presented_separately(app):
    doc = station_doc(
        entrances=("A",),
        passages=[{"from": "entrance:A", "to": "platform:P1"}],
    )
    publish_doc(app, doc)
    body = api_station(app, "T1").get_json()
    assert body["platforms"][0]["status"] == "unreachable"
    assert body["platforms"][0]["entrance_to_platform"]["possible"] is True
    assert body["platforms"][0]["platform_to_entrance"]["possible"] is False


def test_publish_consumes_drafts(app):
    """After a successful publish the drafts are folded into the version."""

    publish_doc(app, two_entrance_doc())
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            assert db.list_drafts(conn) == []
        finally:
            conn.close()

    # Re-uploading a changed drawing shows up only after another publish.
    changed = two_entrance_doc()
    changed["name"] = "新名字"
    client = app.test_client()
    client.post(
        "/admin/upload", data={"document": json.dumps(changed)}, follow_redirects=True
    )
    assert api_station(app, "T1").get_json()["name"] == "T1"
    client.post("/admin/publish", data={}, follow_redirects=True)
    assert api_station(app, "T1").get_json()["name"] == "新名字"


def test_old_version_rows_are_retained(app):
    publish_doc(app, two_entrance_doc())
    first = active_version(app)
    publish_doc(app, two_entrance_doc())
    second = active_version(app)
    assert second > first
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            maps = db.list_station_maps(conn, first)
            assert [m["station_id"] for m in maps] == ["T1"]
        finally:
            conn.close()


def test_station_json_matches_api(app):
    publish_doc(app, two_entrance_doc())
    with_outages(app, ["E1", "E2"])
    api = api_station(app, "T1").get_json()
    page = app.test_client().get("/stations/T1").data.decode("utf-8")
    assert "最小复运组合" in page
    assert api["platforms"][0]["entrance_to_platform"][
        "minimal_recovery_sets"
    ] == [["E1"], ["E2"]]


def test_html_escapes_untrusted_names(app):
    doc = station_doc(
        name='<script>alert("x")</script>',
        passages=[
            {"from": "entrance:A", "to": "platform:P1"},
            {"from": "platform:P1", "to": "entrance:A"},
        ],
    )
    publish_doc(app, doc)
    page = app.test_client().get("/stations/T1").data.decode("utf-8")
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


# ---------------------------------------------------------------------------
# Recalculation of the active version's combination tables
# ---------------------------------------------------------------------------

def test_recalculate_unavailable_without_active_version(client):
    resp = client.post("/admin/recalculate", follow_redirects=True)
    assert "还没有生效版本".encode() in resp.data


def test_recalculate_repairs_tables_of_active_version(app, monkeypatch):
    # Publish first while calibration produces deliberately wrong tables,
    # then recalculate with the real calibration: the correction must land
    # in a new version built from the same original drawings.
    publish_doc(app, two_entrance_doc())
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            first = db.active_version_id(conn)
            maps = db.list_station_maps(conn, first)
        finally:
            conn.close()

    wrong_tables = {
        "P1": {
            "entrance_to_platform": {"possible": False, "sets": None},
            "platform_to_entrance": {"possible": False, "sets": None},
        }
    }
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            with conn:
                conn.execute(
                    "UPDATE station_maps SET tables = ? WHERE version_id = ?",
                    (model.dumps(wrong_tables), first),
                )
        finally:
            conn.close()

    body = api_station(app, "T1").get_json()
    assert body["platforms"][0]["status"] == "unreachable"

    # A draft sitting in staging must survive the recalculation untouched.
    other = station_doc(station_id="T2", entrances=("X",), platforms=("Q1",))
    other["passages"] = [
        {"id": "p", "from": "entrance:X", "to": "platform:Q1"},
        {"id": "q", "from": "platform:Q1", "to": "entrance:X"},
    ]
    client = app.test_client()
    client.post(
        "/admin/upload", data={"document": json.dumps(other)}, follow_redirects=True
    )

    resp = client.post("/admin/recalculate", follow_redirects=True)
    assert "重算".encode() in resp.data
    assert active_version(app) != first

    body = api_station(app, "T1").get_json()
    platform = body["platforms"][0]
    assert platform["status"] == "ok"
    assert platform["entrance_to_platform"]["minimal_elevator_sets"] == [
        ["E1"], ["E2"]
    ]

    # The draft is still staged for a normal publish and did not leak into
    # the recalculated version.
    assert api_station(app, "T2").status_code == 404
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            drafts = db.list_drafts(conn)
            assert [d["station_id"] for d in drafts] == ["T2"]
            maps = db.list_station_maps(conn, db.active_version_id(conn))
            assert [m["station_id"] for m in maps] == ["T1"]
        finally:
            conn.close()


def test_recalculate_uses_current_outage_list_without_touching_it(app):
    publish_doc(app, two_entrance_doc())
    before_version = active_version(app)
    with_outages(app, ["E1", "E2"])

    resp = app.test_client().post("/admin/recalculate", follow_redirects=True)
    assert resp.status_code == 200
    assert active_version(app) != before_version

    body = api_station(app, "T1").get_json()
    assert body["outages_applied"] == ["E1", "E2"]
    assert body["platforms"][0]["status"] == "blocked"

    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            assert db.outage_set(conn) == {"E1", "E2"}
        finally:
            conn.close()


def test_recalculate_discards_result_when_version_changed(app, monkeypatch):
    publish_doc(app, two_entrance_doc())
    first = active_version(app)

    # A second, already-finished version that a concurrent operation could
    # switch to while this recalculation is running.
    publish_doc(app, two_entrance_doc())
    second = active_version(app)
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            with conn:
                db.set_setting(conn, "active_version_id", str(first))
        finally:
            conn.close()
    assert active_version(app) == first

    original = model.build_station_tables

    def switch_pointer_then_calibrate(station):
        # Simulate another publish/recalc switching the active pointer while
        # calibration is in progress.
        with app.app_context():
            conn = db.connect(app.config["DATABASE"])
            try:
                with conn:
                    db.set_setting(conn, "active_version_id", str(second))
            finally:
                conn.close()
        return original(station)

    monkeypatch.setattr(model, "build_station_tables", switch_pointer_then_calibrate)
    resp = app.test_client().post("/admin/recalculate", follow_redirects=True)
    assert "请刷新".encode() in resp.data
    # The concurrent pointer stays active; no extra recalculation version was
    # written and the log records the discarded attempt.
    assert active_version(app) == second
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            log = db.list_publish_log(conn)
            assert log[0]["status"] == "failed"
            assert "重算中止" in log[0]["message"]
        finally:
            conn.close()


def test_recalculate_failure_keeps_active_version_and_logs(app, monkeypatch):
    publish_doc(app, two_entrance_doc())
    first = active_version(app)

    def boom(_station):
        raise RuntimeError("simulated calibration failure")

    monkeypatch.setattr(model, "build_station_tables", boom)
    resp = app.test_client().post("/admin/recalculate", follow_redirects=True)
    assert "继续按当前生效版供数".encode() in resp.data
    assert active_version(app) == first

    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            log = db.list_publish_log(conn)
            assert log[0]["status"] == "failed"
            assert "重算" in log[0]["message"]
        finally:
            conn.close()


def test_recalculate_renders_button_on_publish_page(app):
    publish_doc(app, two_entrance_doc())
    page = app.test_client().get("/admin/publish").data.decode("utf-8")
    assert "/admin/recalculate" in page
    assert "重算" in page


def test_outages_and_queries_work_during_recalculation(app, monkeypatch):
    publish_doc(app, two_entrance_doc())

    original = model.build_station_tables

    def edit_outage_mid_calibration(station):
        # While the whole-map calibration is running, outage registration and
        # navigation queries must keep working off the active version.
        with app.app_context():
            conn = db.connect(app.config["DATABASE"])
            try:
                with conn:
                    db.set_outage(conn, "E1", True, "T1", "检修中")
            finally:
                conn.close()
        client = app.test_client()
        resp = client.get("/api/v1/stations/T1")
        assert resp.status_code == 200
        return original(station)

    monkeypatch.setattr(model, "build_station_tables", edit_outage_mid_calibration)
    resp = app.test_client().post("/admin/recalculate", follow_redirects=True)
    assert resp.status_code == 200

    body = api_station(app, "T1").get_json()
    assert body["outages_applied"] == ["E1"]
    # E2 route keeps the platform reachable under the existential rule.
    assert body["platforms"][0]["status"] == "ok"
