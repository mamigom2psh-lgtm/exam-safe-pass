"""In-memory contest demo scenario sandbox for 시험장 안심패스 플러스.

This module never touches the operational or DEMO SQLite databases, the operational audit log,
or any authentication API. All state lives in process memory and is cleared by reset().
Analysis results here are PRESET demonstration values (source="preset_demo"); they are not
output of any real face or anti-spoofing model, and no outcome here is real authentication.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Callable

BANNER = "공모전 시제품 · 실제 입실 불가"
QR_TTL_SECONDS = 120
MAX_TIME_SHIFT_SECONDS = 3600
MAX_EVENTS = 500

SCENARIOS = {
    "normal_entry": {
        "title": "정상 입실",
        "description": "조회 → 촬영 → 참고 분석 → 감독관 승인 → DEMO QR → 입실",
        "analysis": "reference_only",
    },
    "spoof_suspected": {
        "title": "위조 의심",
        "description": "사전 설정된 '위조 의심' 참고 결과 → 감독관 대면 확인(승인은 서류·얼굴 대조 둘 다 필요)",
        "analysis": "spoof_suspected",
    },
    "expired_qr": {
        "title": "만료 QR",
        "description": "DEMO QR 발급 후 시연용 시계를 앞당겨 만료 차단 확인",
        "analysis": "reference_only",
    },
    "reused_qr": {
        "title": "재사용 QR · 중복 입실",
        "description": "한 번 사용한 QR 재사용과 같은 응시자의 중복 입실 차단 확인",
        "analysis": "reference_only",
    },
    "supervisor_review": {
        "title": "감독관 확인",
        "description": "다른 얼굴 의심 사전 결과 → 감독관이 승인·거부·보류를 사유와 함께 기록",
        "analysis": "face_mismatch_suspected",
    },
    "failure_recovery": {
        "title": "장애 복구",
        "description": "카메라 거부·네트워크·모델 로딩·인쇄 실패 후 복구 (재시도로 QR 중복 발급 없음)",
        "analysis": "reference_only",
    },
}

ANALYSIS_LABELS = {
    "reference_only": "참고용 분석 완료 (사전 설정 · 인증 아님)",
    "spoof_suspected": "위조 의심 (사전 설정 · 확정 아님)",
    "face_mismatch_suspected": "다른 얼굴 의심 (사전 설정 · 확정 아님)",
}
SUSPICIOUS = {"spoof_suspected", "face_mismatch_suspected"}

FAILURES = {
    "camera_denied": "카메라 권한이 거부되었습니다. 브라우저 주소창의 권한 설정에서 카메라를 허용하거나 감독관 확인을 요청하세요.",
    "network": "네트워크 연결에 실패했습니다. 연결을 확인하고 다시 시도하거나 감독관 확인을 요청하세요.",
    "model_load": "분석 모델을 불러오지 못했습니다. 다시 시도하거나 감독관 확인을 요청하세요. 분석 없이 승인되지 않습니다.",
    "print_failure": "인쇄에 실패했습니다. 같은 QR을 다시 출력할 수 있으며 새 QR은 발급되지 않습니다.",
}
REVIEW_DECISIONS = {"approve", "reject", "hold"}
REVIEW_REASONS = {"normal", "document_mismatch", "suspected_proxy", "identity_unverifiable"}
ACTIONS = {"lookup", "capture", "analyze", "review", "issue_qr", "enter", "advance_time", "fail", "recover", "reprint"}
ALLOWED_PARAMS = {
    "review": {"decision", "reason", "document_match", "live_face_match"},
    "advance_time": {"seconds"},
    "fail": {"kind"},
    "recover": {"choice"},
}


class ScenarioError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ScenarioSandbox:
    def __init__(self, qr_factory: Callable[[str], str] | None = None, clock: Callable[[], float] = time.time):
        self._qr_factory = qr_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, dict] = {}
        self._admitted: set[str] = set()  # sandbox-only admissions (per scenario candidate label)
        self._events: list[dict] = []
        self._offset = 0.0
        self._seq = 0

    # ---- helpers -------------------------------------------------------
    def _now(self) -> float:
        return self._clock() + self._offset

    def _log(self, run: dict | None, event: str, outcome: str, detail: str) -> None:
        self._seq += 1
        self._events.append(
            {
                "seq": self._seq,
                "domain": "demo_scenario",
                "scenario": run["scenario"] if run else None,
                "event": event,
                "outcome": outcome,
                "detail": detail,
                "at": round(self._now(), 3),
                "banner": BANNER,
            }
        )
        del self._events[:-MAX_EVENTS]

    def _view(self, run: dict, message: str = "") -> dict:
        qr = run.get("qr")
        return {
            "demo_only": True,
            "real_authentication": False,
            "banner": BANNER,
            "run_id": run["run_id"],
            "scenario": run["scenario"],
            "state": run["state"],
            "steps_done": list(run["steps_done"]),
            "analysis": run.get("analysis"),
            "review": run.get("review"),
            "failure": run.get("failure"),
            "qr_issued": qr is not None,
            "qr_expires_in": None if qr is None else max(0, int(qr["expires_at"] - self._now())),
            "qr_used": bool(qr and qr["used"]),
            "message": message,
        }

    def _get(self, run_id: object) -> dict:
        run = self._runs.get(run_id) if isinstance(run_id, str) else None
        if run is None:
            raise ScenarioError(404, "시연 실행을 찾을 수 없습니다. 시나리오를 다시 시작하세요.")
        return run

    # ---- public API ----------------------------------------------------
    def list_scenarios(self) -> list[dict]:
        return [{"id": key, "title": v["title"], "description": v["description"], "banner": BANNER} for key, v in SCENARIOS.items()]

    def start(self, scenario: object) -> dict:
        if not isinstance(scenario, str) or scenario not in SCENARIOS:
            raise ScenarioError(400, "알 수 없는 시연 시나리오입니다.")
        with self._lock:
            # Restarting a scenario resets its previous run, sandbox admission and QR.
            for run_id in [r for r, v in self._runs.items() if v["scenario"] == scenario]:
                self._admitted.discard(self._runs[run_id]["candidate"])
                del self._runs[run_id]
            run = {
                "run_id": secrets.token_urlsafe(9),
                "scenario": scenario,
                "candidate": f"scenario:{scenario}",
                "state": "lookup",
                "steps_done": [],
                "analysis": None,
                "review": None,
                "failure": None,
                "qr": None,
                "resume_state": None,
            }
            self._runs[run["run_id"]] = run
            self._log(run, "시나리오 시작", "info", SCENARIOS[scenario]["title"])
            return self._view(run, "시연을 시작했습니다. 모든 결과는 사전 설정된 DEMO 값입니다.")

    def reset(self) -> dict:
        with self._lock:
            self._runs.clear()
            self._admitted.clear()
            self._events.clear()
            self._offset = 0.0
        return {"demo_only": True, "banner": BANNER, "message": "시연 시나리오 상태를 모두 초기화했습니다. 운영·DEMO 입실 기록은 변경되지 않았습니다."}

    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events[-200:])

    def act(self, run_id: object, action: object, params: object = None) -> dict:
        if not isinstance(action, str) or action not in ACTIONS:
            raise ScenarioError(400, "허용되지 않은 시연 동작입니다.")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ScenarioError(400, "params는 객체여야 합니다.")
        extra = set(params) - ALLOWED_PARAMS.get(action, set())
        if extra:
            # Browser-supplied scores/flags such as face_match or liveness are never accepted.
            raise ScenarioError(400, "허용되지 않은 필드가 포함되어 있습니다: " + ", ".join(sorted(map(str, extra))[:5]))
        with self._lock:
            run = self._get(run_id)
            return getattr(self, f"_do_{action}")(run, params)

    # ---- actions -------------------------------------------------------
    def _require_state(self, run: dict, *states: str) -> None:
        if run["state"] in {"rejected", "supervisor_pending", "recovery_needed"} and "any" not in states:
            raise ScenarioError(409, f"현재 상태({run['state']})에서는 이 동작을 할 수 없습니다. 감독관 확인 또는 복구를 진행하세요.")
        if run["state"] not in states:
            raise ScenarioError(409, f"현재 상태({run['state']})에서는 이 동작을 할 수 없습니다.")

    def _advance(self, run: dict, step: str, state: str) -> None:
        run["steps_done"].append(step)
        run["state"] = state

    def _do_lookup(self, run, params):
        self._require_state(run, "lookup")
        self._advance(run, "lookup", "capture")
        self._log(run, "등록 정보 조회", "info", "가상 응시자(DEMO)")
        return self._view(run, "등록 정보를 조회했습니다(가상 응시자).")

    def _do_capture(self, run, params):
        self._require_state(run, "capture")
        self._advance(run, "capture", "analysis")
        self._log(run, "촬영", "info", "시연용 가상 촬영")
        return self._view(run, "촬영을 완료했습니다(시연용).")

    def _do_analyze(self, run, params):
        self._require_state(run, "analysis")
        kind = SCENARIOS[run["scenario"]]["analysis"]
        run["analysis"] = {
            "source": "preset_demo",
            "real_model": False,
            "result": kind,
            "label": ANALYSIS_LABELS[kind],
            "suspicious": kind in SUSPICIOUS,
            "is_authentication": False,
        }
        self._advance(run, "analyze", "review")
        self._log(run, "참고 분석(사전 설정)", "info", kind)
        return self._view(run, "참고 분석 결과는 사전 설정된 시연값이며 실제 모델 판정이 아닙니다. 감독관 확인이 필요합니다.")

    def _do_review(self, run, params):
        self._require_state(run, "review")
        decision, reason = params.get("decision"), params.get("reason")
        doc, live = params.get("document_match"), params.get("live_face_match")
        if decision not in REVIEW_DECISIONS or reason not in REVIEW_REASONS or not isinstance(doc, bool) or not isinstance(live, bool):
            raise ScenarioError(400, "결정, 사유, 서류 대조, 현장 얼굴 대조를 모두 지정해야 합니다.")
        suspicious = bool(run["analysis"] and run["analysis"]["suspicious"])
        if decision == "approve" and (not doc or not live):
            decision, outcome_note = "hold", "서류 또는 얼굴 대조가 확인되지 않아 보류로 처리"
        elif decision == "approve" and suspicious and reason != "normal":
            decision, outcome_note = "hold", "의심 결과가 있는 상태에서 사유가 정상이 아니므로 보류"
        else:
            outcome_note = ""
        run["review"] = {"decision": decision, "reason": reason, "at": round(self._now(), 3), "note": outcome_note}
        if decision == "approve":
            self._advance(run, "review", "approved")
            outcome = "accepted"
        elif decision == "reject":
            self._advance(run, "review", "rejected")
            outcome = "rejected"
        else:
            self._advance(run, "review", "supervisor_pending")
            outcome = "pending"
        self._log(run, "감독관 확인", outcome, f"decision={decision}; reason={reason}")
        text = {"approved": "감독관이 승인했습니다. DEMO QR을 발급할 수 있습니다.", "rejected": "감독관이 거부했습니다. QR은 발급되지 않습니다.",
                "supervisor_pending": "보류되었습니다. 책임자 확인 전까지 QR은 발급되지 않습니다."}[run["state"]]
        return self._view(run, (outcome_note + " · " if outcome_note else "") + text)

    def _do_issue_qr(self, run, params):
        if run["qr"] is not None:
            self._log(run, "DEMO QR 발급", "blocked", "중복 발급 시도 차단")
            raise ScenarioError(409, "이미 DEMO QR이 발급되었습니다. 새 QR은 발급되지 않습니다(재출력만 가능).")
        self._require_state(run, "approved")
        token = "DEMO-ONLY:SCENARIO:" + secrets.token_urlsafe(16)
        now = self._now()
        run["qr"] = {"hash": _digest(token), "issued_at": now, "expires_at": now + QR_TTL_SECONDS, "used": False}
        self._advance(run, "issue_qr", "qr_issued")
        self._log(run, "DEMO QR 발급", "accepted", f"ttl={QR_TTL_SECONDS}s")
        view = self._view(run, "DEMO QR을 발급했습니다. 실제 입실에는 사용할 수 없습니다.")
        view["token"] = token
        view["qr_png"] = self._qr_factory(token) if self._qr_factory else None
        view["qr_mime_type"] = "image/png"
        return view

    def _do_reprint(self, run, params):
        qr = run["qr"]
        if qr is None:
            raise ScenarioError(409, "재출력할 QR이 없습니다.")
        if qr["used"] or qr["expires_at"] <= self._now():
            raise ScenarioError(409, "사용되었거나 만료된 QR은 재출력할 수 없습니다.")
        self._log(run, "DEMO QR 재출력", "info", "새 토큰 없음")
        if run["failure"] and run["failure"]["kind"] == "print_failure":
            run["failure"] = None
            run["state"] = run["resume_state"] or "qr_issued"
        return self._view(run, "같은 QR을 다시 출력할 수 있습니다. 새 QR은 발급되지 않았습니다.")

    def _do_enter(self, run, params):
        qr = run["qr"]
        if qr is None or run["state"] not in {"qr_issued", "entered"}:
            raise ScenarioError(409, "발급된 DEMO QR이 없습니다.")
        if run["state"] == "entered" and qr["used"]:
            self._log(run, "DEMO 입실", "blocked", "QR 재사용 차단")
            raise ScenarioError(409, "이미 사용된 QR입니다(재사용 차단). 시나리오가 의도한 차단 시연입니다.")
        if qr["expires_at"] <= self._now():
            self._log(run, "DEMO 입실", "blocked", "QR 만료 차단")
            raise ScenarioError(410, "QR이 만료되었습니다. 만료 차단 시연입니다.")
        if run["candidate"] in self._admitted:
            self._log(run, "DEMO 입실", "blocked", "중복 입실 차단")
            raise ScenarioError(409, "이미 입실 처리된 응시자입니다(중복 입실 차단).")
        qr["used"] = True
        self._admitted.add(run["candidate"])
        self._advance(run, "enter", "entered")
        self._log(run, "DEMO 입실", "accepted", "시연 입실(실제 입실 아님)")
        return self._view(run, "DEMO 입실 시연이 완료되었습니다. 실제 입실이 아닙니다.")

    def _do_advance_time(self, run, params):
        seconds = params.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= MAX_TIME_SHIFT_SECONDS:
            raise ScenarioError(400, f"seconds는 1~{MAX_TIME_SHIFT_SECONDS} 사이여야 합니다.")
        self._offset += float(seconds)
        self._log(run, "시연용 시계 이동", "info", f"+{int(seconds)}s (시연 시계만 이동)")
        return self._view(run, f"시연용 시계를 {int(seconds)}초 앞당겼습니다.")

    def _do_fail(self, run, params):
        kind = params.get("kind")
        if kind not in FAILURES:
            raise ScenarioError(400, "알 수 없는 장애 유형입니다.")
        if run["state"] in {"rejected", "entered"}:
            raise ScenarioError(409, "종료된 시연에는 장애를 주입할 수 없습니다.")
        if kind == "print_failure" and run["qr"] is None:
            raise ScenarioError(409, "인쇄 실패는 QR 발급 후에만 시연할 수 있습니다.")
        run["resume_state"] = run["state"] if run["state"] != "recovery_needed" else run["resume_state"]
        run["failure"] = {"kind": kind, "guidance": FAILURES[kind]}
        if kind != "print_failure":
            run["state"] = "recovery_needed"
        self._log(run, "장애 주입(시연)", "blocked", kind)
        return self._view(run, FAILURES[kind])

    def _do_recover(self, run, params):
        choice = params.get("choice")
        if run["failure"] is None:
            raise ScenarioError(409, "복구할 장애가 없습니다.")
        if choice == "retry":
            if run["failure"]["kind"] == "print_failure":
                raise ScenarioError(409, "인쇄 실패는 '재출력'으로 복구하세요.")
            run["state"] = run["resume_state"] or "lookup"
            run["failure"] = None
            self._log(run, "장애 복구", "info", "재시도")
            return self._view(run, "재시도할 수 있습니다. 실패한 세션은 자동 승인되지 않았습니다.")
        if choice == "supervisor":
            run["state"] = "supervisor_pending"
            run["failure"] = None
            self._log(run, "장애 복구", "pending", "감독관 확인 경로")
            return self._view(run, "감독관 확인으로 안내합니다. QR은 발급되지 않았습니다.")
        raise ScenarioError(400, "복구 방법은 retry 또는 supervisor여야 합니다.")
