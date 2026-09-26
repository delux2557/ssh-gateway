"""HTTP surface: envelope, status codes, auth, SSE, jobs. Runs against FakeBackend."""

from __future__ import annotations

import time

from conftest import parse_sse

from ssh_gateway.backend import ChannelBusy
from ssh_gateway.policy import CommandPolicy


def strict_policy(center, mode="whitelist"):
    """Replace the policy with one that allows nothing (empty whitelist)."""
    center.policy = CommandPolicy(mode=mode, whitelist_file="", dangerous_patterns=[],
                                  allow_everything_when_no_whitelist=False)


# ----------------------------------------------------------------------
# Envelope and routing
# ----------------------------------------------------------------------
def test_run_returns_envelope_and_duration(api):
    r = api("POST", "/run", {"cmd": "uname -a"})
    assert r.status == 200
    body = r.json()
    assert body["ok"] is True
    assert body["code"] == ""
    assert body["data"]["cmd"] == "uname -a"
    assert body["data"]["duration_ms"] >= 0


def test_unknown_route_is_404_with_envelope(api):
    r = api("GET", "/nope")
    assert r.status == 404
    assert r.json()["code"] == "NOT_FOUND"


def test_malformed_body_is_400(api):
    r = api("POST", "/run", b"{not json")
    assert r.status == 400
    assert r.json()["code"] == "BAD_REQUEST"


def test_missing_cmd_is_400(api):
    r = api("POST", "/run", {})
    assert r.status == 400
    assert "cmd" in r.json()["error"]


def test_openapi_paths_are_all_routed(api):
    spec = api("GET", "/openapi.json").json()
    routes = {row["path"] for row in api("GET", "/routes").json()["data"]["routes"]}
    assert set(spec["paths"]) <= routes
    assert "/job/{id}/kill" in routes


def test_index_is_html(api):
    r = api("GET", "/")
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/html")
    assert "/run/stream" in r.text


# ----------------------------------------------------------------------
# Error -> status mapping
# ----------------------------------------------------------------------
def test_channel_busy_maps_to_503_with_retry_after(api, center):
    center.backend.exec_error = ChannelBusy("all 8 SSH channels are busy")
    r = api("POST", "/run", {"cmd": "ls"})
    assert r.status == 503
    assert int(r.headers["Retry-After"]) > 0
    assert r.json()["code"] == "UNAVAILABLE"


def test_missing_remote_path_maps_to_404(api):
    assert api("POST", "/sftp/list", {"remote": "/does/not/exist"}).status == 404


def test_backend_failure_maps_to_502(api, center):
    center.backend.exec_error = OSError("socket closed")
    r = api("POST", "/run", {"cmd": "ls"})
    assert r.status == 502
    assert r.json()["code"] == "REMOTE_ERROR"


def test_policy_denial_maps_to_403(api, center):
    strict_policy(center)
    r = api("POST", "/run", {"cmd": "ls"})
    assert r.status == 403
    assert r.json()["code"] == "FORBIDDEN"


def test_empty_remote_dir_name_is_400(api):
    assert api("POST", "/sftp/list", {}).status == 400


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------
def test_token_gates_private_and_public_paths(serve, make_center):
    center = make_center(api_token="secret")
    http = serve(center)
    assert http("POST", "/run", {"cmd": "ls"}).status == 401
    assert http("POST", "/run", {"cmd": "ls"},
                headers={"Authorization": "Bearer nope"}).status == 401
    assert http("POST", "/run", {"cmd": "ls"},
                headers={"Authorization": "Bearer secret"}).status == 200
    # docs and probes stay reachable; the landing page names the device, so it does not
    assert http("GET", "/health").status == 200
    assert http("GET", "/openapi.json").status == 200
    assert http("GET", "/routes").status == 200
    assert http("GET", "/").status == 401


def test_health_hides_target_when_auth_on(api, center):
    center.auth.token = "secret"
    center.auth.enabled = True
    assert "target" not in api("GET", "/health").json()["data"]


def test_health_shows_target_without_auth(api):
    assert "user" in api("GET", "/health").json()["data"]["target"]


def test_auth_failure_is_audited(api, center):
    center.auth.token = "secret"
    center.auth.enabled = True
    assert api("POST", "/run", {"cmd": "ls"}).status == 401
    entries = api("GET", "/audit",
                  headers={"Authorization": "Bearer secret"}).json()["data"]["entries"]
    assert [e for e in entries if e["action"] == "auth_fail"]


# ----------------------------------------------------------------------
# SSE
# ----------------------------------------------------------------------
def test_run_stream_emits_started_stdout_exit(api):
    r = api("POST", "/run/stream", {"cmd": "sleep 1"})
    assert r.headers["Content-Type"].startswith("text/event-stream")
    events = dict(parse_sse(r.text))
    assert events["started"]["cmd"] == "sleep 1"
    assert events["stdout"]["text"] == "out-1\n"
    assert events["exit"]["exit_code"] == 0


def test_run_stream_reports_denial_as_error_event(api, center):
    strict_policy(center)
    names = [name for name, _ in parse_sse(api("POST", "/run/stream", {"cmd": "rm -rf /"}).text)]
    assert names == ["started", "error"]


# ----------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------
def wait_for_job(http, job_id, statuses=("done", "failed", "killed")):
    for _ in range(100):
        data = http("GET", f"/job/{job_id}").json()["data"]
        if data["status"] in statuses:
            return data
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still {data['status']}")


def test_async_job_roundtrip(api):
    r = api("POST", "/run/async", {"cmd": "long-task"})
    assert r.status == 202
    job_id = r.json()["data"]["job_id"]
    assert wait_for_job(api, job_id)["status"] == "done"
    out = api("GET", f"/job/{job_id}/output").json()["data"]
    assert out["tail"] == "started:long-task\n"
    assert any(j["job_id"] == job_id for j in api("GET", "/jobs").json()["data"]["jobs"])


def test_job_output_tail_and_stderr_stream(api):
    job_id = api("POST", "/run/async", {"cmd": "abc"}).json()["data"]["job_id"]
    wait_for_job(api, job_id)
    assert api("GET", f"/job/{job_id}/output?tail=4").json()["data"]["tail"] == "abc\n"
    assert api("GET", f"/job/{job_id}/output?stream=stderr").status == 200


def test_job_404_for_unknown_id(api):
    assert api("GET", "/job/nope").status == 404
    assert api("GET", "/job/nope/output").status == 404


def test_kill_finished_job_reports_honestly(api):
    job_id = api("POST", "/run/async", {"cmd": "x"}).json()["data"]["job_id"]
    wait_for_job(api, job_id)
    result = api("POST", f"/job/{job_id}/kill").json()["data"]
    assert result == {"job_id": job_id, "killed": False, "reason": "already finished"}


def test_follow_job_output_ends_with_exit(api):
    job_id = api("POST", "/run/async", {"cmd": "watchme"}).json()["data"]["job_id"]
    frames = parse_sse(api("GET", f"/job/{job_id}/output?follow=1").text)
    assert [name for name, _ in frames][-1] == "exit"


# ----------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------
def test_run_is_audited_with_client_and_cmd(api):
    api("POST", "/run", {"cmd": "id"})
    entries = api("GET", "/audit").json()["data"]["entries"]
    run = [e for e in entries if e["action"] == "run"]
    assert run[-1]["cmd"] == "id"
    assert run[-1]["client"]


def test_denied_command_never_reaches_backend(api, center):
    center.policy = CommandPolicy(mode="blacklist", whitelist_file="",
                                  dangerous_patterns=["rm -rf"],
                                  allow_everything_when_no_whitelist=True)
    before = len(center.backend.exec_calls)
    assert api("POST", "/run", {"cmd": "rm -rf /"}).status == 403
    assert len(center.backend.exec_calls) == before
    denied = [e for e in api("GET", "/audit").json()["data"]["entries"]
              if e["result"] == "denied"]
    assert denied


def test_audit_filter_by_action(api):
    api("POST", "/run", {"cmd": "uptime"})
    entries = api("GET", "/audit?action=run").json()["data"]["entries"]
    assert entries and all(e["action"] == "run" for e in entries)


# ----------------------------------------------------------------------
# Transport control
# ----------------------------------------------------------------------
def test_close_drops_transport(api, center):
    assert api("POST", "/close").json()["data"] == {"closed": True}
    assert center.backend.closed == 1
