
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any, Optional, Tuple
import os
import re
import math
import pandas as pd


# =========================================================
# 데이터 구조
# =========================================================

@dataclass
class EvidenceRecord:
    """
    수집 결과를 분석 엔진에 넣기 위한 표준 증거 레코드
    """
    source_type: str                       # kc, rra, kipris, dart, tipa, koraia, gs, nep, procurement, seller_page, ocr_text ...
    text: str = ""                        # 설명 텍스트 / 검색 결과 텍스트 / 특허 제목/요약 등
    scope: str = "company"                # company | product | model | product_or_model
    title: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    # 이미 수집 파이프라인에서 정리한 힌트들
    matched_company: bool = False
    matched_product: bool = False
    matched_model: bool = False

    # 옵션: 특정 requirement/component를 이미 직접 매칭했으면 사용
    matched_components: List[str] = field(default_factory=list)


@dataclass
class CapabilityScore:
    capability_id: str
    capability_name_ko: str
    base_claim_score: float
    requirement_score: float
    source_quality_score: float
    confusion_penalty: float
    company_only_penalty: float
    scope_bonus: float
    final_score: float
    positive_claim: bool
    required_fulfillment_ratio: float
    optional_fulfillment_ratio: float
    matched_strong_patterns: List[str] = field(default_factory=list)
    matched_weak_patterns: List[str] = field(default_factory=list)
    matched_negative_patterns: List[str] = field(default_factory=list)
    supporting_sources: List[str] = field(default_factory=list)
    fulfilled_required_components: List[str] = field(default_factory=list)
    fulfilled_optional_components: List[str] = field(default_factory=list)
    missing_required_components: List[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    accs: float
    raw_accs: float
    hes: float
    tes: float
    ces: float
    ecs: float
    conf: float
    verdict: str
    risk_level: str
    top_capabilities: List[Dict[str, Any]]
    reasons: List[str]
    capability_scores: List[Dict[str, Any]]
    details: Dict[str, Any]


# =========================================================
# 공통 유틸
# =========================================================

def clamp(v: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, v))


def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def normalize_text(text: str) -> str:
    text = str(text or "")
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def unique_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# =========================================================
# 온톨로지 로더
# =========================================================

class OntologyRepository:
    def __init__(self, ontology_dir: str):
        self.ontology_dir = ontology_dir
        self._load()

    def _read_csv(self, filename: str) -> pd.DataFrame:
        path = os.path.join(self.ontology_dir, filename)
        if not os.path.exists(path):
            raise FileNotFoundError(f"온톨로지 파일을 찾을 수 없습니다: {path}")
        return pd.read_csv(path)

    def _load(self) -> None:
        self.cap_df = self._read_csv("ai_capability_master.csv")
        self.req_df = self._read_csv("capability_requirement_master.csv")
        self.confusion_df = self._read_csv("confusion_rule_master.csv")
        self.pattern_df = self._read_csv("evidence_pattern_master.csv")
        self.req_map_df = self._read_csv("requirement_evidence_map_master.csv")
        self.source_df = self._read_csv("source_credibility_master.csv")
        self.neg_df = self._read_csv("negative_pattern_master.csv")
        self.rule_df = self._read_csv("capability_scoring_rule_master.csv")

        self.capability_map = {
            row["capability_id"]: row.to_dict()
            for _, row in self.cap_df.iterrows()
        }

        self.requirements_by_cap = {
            cap_id: grp.to_dict(orient="records")
            for cap_id, grp in self.req_df.groupby("capability_id")
        }

        self.patterns_by_cap = {
            cap_id: grp.to_dict(orient="records")
            for cap_id, grp in self.pattern_df.groupby("capability_id")
        }

        self.negative_by_cap = {
            cap_id: grp.to_dict(orient="records")
            for cap_id, grp in self.neg_df.groupby("applies_to_capability_id")
        }

        self.confusion_by_cap = {
            cap_id: grp.to_dict(orient="records")
            for cap_id, grp in self.confusion_df.groupby("capability_id")
        }

        self.req_map_by_cap = {
            cap_id: grp.to_dict(orient="records")
            for cap_id, grp in self.req_map_df.groupby("capability_id")
        }

        self.source_rule_map = {
            row["source_type"]: row.to_dict()
            for _, row in self.source_df.iterrows()
        }

        self.scoring_rule_map = {
            row["capability_id"]: row.to_dict()
            for _, row in self.rule_df.iterrows()
        }

    def get_capability_ids(self) -> List[str]:
        return list(self.capability_map.keys())

    def get_capability_name(self, capability_id: str) -> str:
        return self.capability_map.get(capability_id, {}).get("capability_name_ko", capability_id)

    def get_requirements(self, capability_id: str) -> List[Dict[str, Any]]:
        return self.requirements_by_cap.get(capability_id, [])

    def get_patterns(self, capability_id: str) -> List[Dict[str, Any]]:
        return self.patterns_by_cap.get(capability_id, [])

    def get_negative_patterns(self, capability_id: str) -> List[Dict[str, Any]]:
        return self.negative_by_cap.get(capability_id, [])

    def get_confusion_rules(self, capability_id: str) -> List[Dict[str, Any]]:
        return self.confusion_by_cap.get(capability_id, [])

    def get_requirement_maps(self, capability_id: str) -> List[Dict[str, Any]]:
        return self.req_map_by_cap.get(capability_id, [])

    def get_source_rule(self, source_type: str) -> Dict[str, Any]:
        return self.source_rule_map.get(source_type, {
            "source_type": source_type,
            "credibility_weight": 0.50,
            "directness_base_weight": 0.50,
            "update_reliability_weight": 0.50,
            "source_level": "unknown",
            "source_name_ko": source_type,
            "description_ko": ""
        })

    def get_scoring_rule(self, capability_id: str) -> Dict[str, Any]:
        return self.scoring_rule_map.get(capability_id, {
            "capability_id": capability_id,
            "required_fulfillment_weight": 0.60,
            "optional_fulfillment_weight": 0.15,
            "strong_pattern_weight": 0.15,
            "weak_pattern_weight": 0.05,
            "source_quality_weight": 0.10,
            "required_threshold_for_positive": 0.70,
            "max_optional_bonus": 15,
            "confusion_penalty": 20,
            "company_only_penalty": 12,
            "product_level_bonus": 8,
            "model_level_bonus": 12,
            "min_evidence_sources_for_high_confidence": 2,
            "note_ko": "기본 규칙"
        })


# =========================================================
# 분석 엔진
# =========================================================

class OntologyAnalysisEngine:
    """
    임시 점수 로직을 완전히 대체하는 온톨로지 기반 분석 엔진
    """

    HES_SOURCES = {"kc", "rra"}
    TES_SOURCES = {"kipris", "dart"}
    CES_SOURCES = {"tipa", "koraia", "gs", "nep", "procurement"}

    def __init__(self, ontology_dir: str):
        self.repo = OntologyRepository(ontology_dir)

    # -----------------------------------------------------
    # 공개 메인 함수
    # -----------------------------------------------------
    def analyze(
        self,
        evidence_records: List[EvidenceRecord],
        ad_text: str = "",
        ocr_text: str = "",
        extra_texts: Optional[List[str]] = None,
    ) -> AnalysisResult:
        claim_text = self._build_claim_text(ad_text, ocr_text, extra_texts, evidence_records)
        capability_results = self._score_all_capabilities(evidence_records, claim_text)

        # 긍정 capability만 우선 사용
        positive_caps = [c for c in capability_results if c.positive_claim]
        used_caps = positive_caps if positive_caps else capability_results

        hes = self._aggregate_channel_score(used_caps, self.HES_SOURCES)
        tes = self._aggregate_channel_score(used_caps, self.TES_SOURCES)
        ces = self._aggregate_channel_score(used_caps, self.CES_SOURCES)

        h_found = 1 if hes > 0 else 0
        t_found = 1 if tes > 0 else 0
        c_found = 1 if ces > 0 else 0

        ecs = round(((h_found + t_found + c_found) / 3.0) * 100.0, 2)

        # 네가 쓰던 구조 유지: 존재하는 채널만 RawACCS 계산
        wh, wt, wc = 0.35, 0.40, 0.25
        numerator = (wh * hes * h_found) + (wt * tes * t_found) + (wc * ces * c_found)
        denominator = (wh * h_found) + (wt * t_found) + (wc * c_found)
        raw_accs = round(numerator / denominator, 2) if denominator else 0.0

        # ECS 반영
        alpha = 0.85
        accs = round(clamp(alpha * raw_accs + (1 - alpha) * ecs), 2)

        conf = self._calculate_confidence(used_caps, evidence_records, h_found, t_found, c_found)
        verdict, risk_level = self._decide_verdict(accs, conf, used_caps)

        top_caps = sorted(used_caps, key=lambda x: x.final_score, reverse=True)[:5]
        reasons = self._build_reasons(accs, raw_accs, hes, tes, ces, ecs, conf, verdict, risk_level, top_caps, used_caps)

        return AnalysisResult(
            accs=accs,
            raw_accs=raw_accs,
            hes=round(hes, 2),
            tes=round(tes, 2),
            ces=round(ces, 2),
            ecs=ecs,
            conf=conf,
            verdict=verdict,
            risk_level=risk_level,
            top_capabilities=[asdict(c) for c in top_caps],
            reasons=reasons,
            capability_scores=[asdict(c) for c in sorted(capability_results, key=lambda x: x.final_score, reverse=True)],
            details={
                "claim_text": claim_text,
                "alpha": alpha,
                "channel_presence": {
                    "hardware": h_found,
                    "technical": t_found,
                    "certification": c_found,
                },
                "evidence_count": len(evidence_records),
                "evidence_by_source": self._count_evidence_by_source(evidence_records),
            }
        )

    # -----------------------------------------------------
    # Capability별 점수 계산
    # -----------------------------------------------------
    def _score_all_capabilities(self, evidence_records: List[EvidenceRecord], claim_text: str) -> List[CapabilityScore]:
        scores: List[CapabilityScore] = []
        for cap_id in self.repo.get_capability_ids():
            scores.append(self._score_one_capability(cap_id, evidence_records, claim_text))
        return scores

    def _score_one_capability(self, capability_id: str, evidence_records: List[EvidenceRecord], claim_text: str) -> CapabilityScore:
        cap_name = self.repo.get_capability_name(capability_id)
        scoring_rule = self.repo.get_scoring_rule(capability_id)

        # 1) claim pattern 매칭
        strong_patterns, weak_patterns = self._match_positive_patterns(capability_id, claim_text)
        negative_patterns = self._match_negative_patterns(capability_id, claim_text)

        strong_hits = len(strong_patterns)
        weak_hits = len(weak_patterns)

        strong_pattern_score = min(100.0, strong_hits * 35.0)
        weak_pattern_score = min(100.0, weak_hits * 20.0)

        base_claim_score = (
            strong_pattern_score * float(scoring_rule["strong_pattern_weight"]) +
            weak_pattern_score * float(scoring_rule["weak_pattern_weight"])
        ) / max(
            1e-9,
            float(scoring_rule["strong_pattern_weight"]) + float(scoring_rule["weak_pattern_weight"])
        )

        # 2) requirement fulfillment
        req_score_info = self._calculate_requirement_score(capability_id, evidence_records)

        # 3) source quality
        source_quality_score = self._calculate_source_quality(req_score_info["supporting_evidence"])

        # 4) penalty / bonus
        confusion_penalty = 0.0
        if negative_patterns:
            base_penalty = float(scoring_rule["confusion_penalty"])
            penalty_ratio = min(1.0, sum(p["penalty_weight"] for p in negative_patterns))
            confusion_penalty = base_penalty * penalty_ratio

        scope_bonus = 0.0
        company_only_penalty = 0.0
        scope_types = {ev.scope for ev in req_score_info["supporting_evidence"]}

        if "model" in scope_types:
            scope_bonus += float(scoring_rule["model_level_bonus"])
        elif "product" in scope_types or "product_or_model" in scope_types:
            scope_bonus += float(scoring_rule["product_level_bonus"])
        elif scope_types == {"company"} and req_score_info["supporting_evidence"]:
            company_only_penalty += float(scoring_rule["company_only_penalty"])

        # 5) 최종 capability score
        required_weight = float(scoring_rule["required_fulfillment_weight"])
        optional_weight = float(scoring_rule["optional_fulfillment_weight"])
        quality_weight = float(scoring_rule["source_quality_weight"])

        req_component_score = (
            req_score_info["required_ratio"] * 100.0 * required_weight +
            req_score_info["optional_ratio"] * 100.0 * optional_weight +
            source_quality_score * quality_weight
        ) / max(1e-9, required_weight + optional_weight + quality_weight)

        final_score = clamp(
            0.45 * base_claim_score +
            0.55 * req_component_score -
            confusion_penalty -
            company_only_penalty +
            scope_bonus
        )

        required_threshold = float(scoring_rule["required_threshold_for_positive"])
        positive_claim = (
            (strong_hits > 0 or weak_hits > 0) and
            req_score_info["required_ratio"] >= required_threshold * 0.5
        )

        return CapabilityScore(
            capability_id=capability_id,
            capability_name_ko=cap_name,
            base_claim_score=round(base_claim_score, 2),
            requirement_score=round(req_component_score, 2),
            source_quality_score=round(source_quality_score, 2),
            confusion_penalty=round(confusion_penalty, 2),
            company_only_penalty=round(company_only_penalty, 2),
            scope_bonus=round(scope_bonus, 2),
            final_score=round(final_score, 2),
            positive_claim=positive_claim,
            required_fulfillment_ratio=round(req_score_info["required_ratio"], 4),
            optional_fulfillment_ratio=round(req_score_info["optional_ratio"], 4),
            matched_strong_patterns=strong_patterns,
            matched_weak_patterns=weak_patterns,
            matched_negative_patterns=[p["pattern_text_ko"] for p in negative_patterns],
            supporting_sources=unique_keep_order([ev.source_type for ev in req_score_info["supporting_evidence"]]),
            fulfilled_required_components=req_score_info["fulfilled_required_components"],
            fulfilled_optional_components=req_score_info["fulfilled_optional_components"],
            missing_required_components=req_score_info["missing_required_components"],
        )

    # -----------------------------------------------------
    # 텍스트/패턴
    # -----------------------------------------------------
    def _build_claim_text(
        self,
        ad_text: str,
        ocr_text: str,
        extra_texts: Optional[List[str]],
        evidence_records: List[EvidenceRecord],
    ) -> str:
        parts = [ad_text or "", ocr_text or ""]
        if extra_texts:
            parts.extend(extra_texts)
        for ev in evidence_records:
            if ev.source_type in {"seller_page", "ocr_text", "product_text"} and ev.text:
                parts.append(ev.text)
        return normalize_text(" ".join(parts))

    def _match_positive_patterns(self, capability_id: str, claim_text: str) -> Tuple[List[str], List[str]]:
        strong, weak = [], []
        for row in self.repo.get_patterns(capability_id):
            pattern = normalize_text(row.get("pattern_text_ko", ""))
            if pattern and pattern in claim_text:
                if str(row.get("evidence_strength", "")).lower() == "strong":
                    strong.append(row["pattern_text_ko"])
                else:
                    weak.append(row["pattern_text_ko"])
        return unique_keep_order(strong), unique_keep_order(weak)

    def _match_negative_patterns(self, capability_id: str, claim_text: str) -> List[Dict[str, Any]]:
        matched = []
        for row in self.repo.get_negative_patterns(capability_id):
            pattern = normalize_text(row.get("pattern_text_ko", ""))
            if pattern and pattern in claim_text:
                matched.append(row)
        return matched

    # -----------------------------------------------------
    # Requirement / evidence 매핑
    # -----------------------------------------------------
    def _calculate_requirement_score(self, capability_id: str, evidence_records: List[EvidenceRecord]) -> Dict[str, Any]:
        requirements = self.repo.get_requirements(capability_id)
        req_maps = self.repo.get_requirement_maps(capability_id)

        # requirement key -> req info
        required_components = []
        optional_components = []
        fulfilled_required = []
        fulfilled_optional = []
        supporting_evidence: List[EvidenceRecord] = []

        for req in requirements:
            comp_name = req["component_name_ko"]
            required_level = str(req["required_level"]).lower().strip()
            matched_evidence = self._find_evidence_for_requirement(capability_id, comp_name, required_level, req_maps, evidence_records)

            if required_level == "required":
                required_components.append(comp_name)
                if matched_evidence:
                    fulfilled_required.append(comp_name)
                    supporting_evidence.extend(matched_evidence)
            else:
                optional_components.append(comp_name)
                if matched_evidence:
                    fulfilled_optional.append(comp_name)
                    supporting_evidence.extend(matched_evidence)

        supporting_evidence = self._dedup_evidence(supporting_evidence)
        required_ratio = safe_div(len(fulfilled_required), len(required_components))
        optional_ratio = safe_div(len(fulfilled_optional), len(optional_components))

        return {
            "required_ratio": required_ratio,
            "optional_ratio": optional_ratio,
            "fulfilled_required_components": fulfilled_required,
            "fulfilled_optional_components": fulfilled_optional,
            "missing_required_components": [c for c in required_components if c not in fulfilled_required],
            "supporting_evidence": supporting_evidence,
        }

    def _find_evidence_for_requirement(
        self,
        capability_id: str,
        component_name: str,
        required_level: str,
        req_maps: List[Dict[str, Any]],
        evidence_records: List[EvidenceRecord]
    ) -> List[EvidenceRecord]:
        matched = []

        candidate_maps = [
            m for m in req_maps
            if m["component_name_ko"] == component_name and str(m["required_level"]).lower().strip() == required_level
        ]

        for ev in evidence_records:
            ev_text = normalize_text(ev.text)
            source_rule = self.repo.get_source_rule(ev.source_type)

            for m in candidate_maps:
                if ev.source_type != m["acceptable_evidence_source"]:
                    continue

                # minimum strength 해석
                min_strength = str(m.get("minimum_strength", "weak")).lower().strip()
                if min_strength == "strong":
                    # 강한 근거는 제품/모델 매칭이나 명시 component 매칭이 있거나,
                    # 텍스트에 component명이 포함되어야 인정
                    if not self._match_requirement_strong(component_name, ev, ev_text):
                        continue
                else:
                    if not self._match_requirement_weak(component_name, ev, ev_text):
                        continue

                # scope 검사
                if not self._scope_compatible(m.get("match_scope", ""), ev.scope):
                    continue

                matched.append(ev)

        return self._dedup_evidence(matched)

    def _match_requirement_strong(self, component_name: str, ev: EvidenceRecord, ev_text: str) -> bool:
        component_tokens = self._component_aliases(component_name)

        if component_name in ev.matched_components:
            return True
        if any(token in ev_text for token in component_tokens):
            return True
        if ev.matched_model or ev.matched_product:
            # 제품/모델 매칭이 있으면 구조 근거로 부분 인정
            return True
        return False

    def _match_requirement_weak(self, component_name: str, ev: EvidenceRecord, ev_text: str) -> bool:
        component_tokens = self._component_aliases(component_name)
        if component_name in ev.matched_components:
            return True
        if any(token in ev_text for token in component_tokens):
            return True
        if ev.matched_company or ev.matched_product or ev.matched_model:
            return True
        return False

    def _component_aliases(self, component_name: str) -> List[str]:
        base = normalize_text(component_name)
        aliases = [base]

        alias_map = {
            "카메라 센서": ["카메라", "영상", "image sensor", "camera", "센서"],
            "객체 감지 모델": ["객체 감지", "물체 감지", "사물 인식", "object detection"],
            "객체 추적 모듈": ["객체 추적", "tracking", "track"],
            "사람 탐지 모델": ["사람 감지", "인체 감지", "human detection", "person detection"],
            "얼굴 탐지 모델": ["얼굴 감지", "face detection"],
            "얼굴 임베딩/매칭 모듈": ["얼굴 인식", "face recognition", "식별", "매칭"],
            "마이크": ["마이크", "microphone", "mic"],
            "음성 인식 모델": ["음성 인식", "speech recognition", "stt", "음성 분석"],
            "화자 식별/인증 모듈": ["화자 인식", "speaker recognition", "화자 식별", "voice biometrics"],
            "이상 탐지 모델": ["이상 탐지", "anomaly detection", "이상 감지"],
            "행동 분석 모델": ["행동 분석", "행동 인식", "activity recognition"],
            "추천 모델": ["추천", "recommendation", "personalized", "개인화 추천"],
            "실시간 위치/지도 정보": ["지도", "경로", "위치", "맵", "gps"],
            "경로 계획 알고리즘": ["경로 계획", "path planning", "route planning"],
            "장애물 감지 센서": ["장애물 감지", "obstacle", "라이다", "lidar", "초음파", "ultrasonic"],
        }
        aliases.extend(alias_map.get(component_name, []))
        return [normalize_text(a) for a in aliases]

    def _scope_compatible(self, required_scope: str, ev_scope: str) -> bool:
        required_scope = str(required_scope or "").strip().lower()
        ev_scope = str(ev_scope or "").strip().lower()

        if required_scope == "" or required_scope == "any":
            return True
        if required_scope == ev_scope:
            return True
        if required_scope == "product_or_model" and ev_scope in {"product", "model", "product_or_model"}:
            return True
        if required_scope == "product" and ev_scope in {"product", "model"}:
            return True
        return False

    def _dedup_evidence(self, records: List[EvidenceRecord]) -> List[EvidenceRecord]:
        out = []
        seen = set()
        for ev in records:
            key = (ev.source_type, ev.scope, ev.title, ev.text[:100])
            if key not in seen:
                seen.add(key)
                out.append(ev)
        return out

    # -----------------------------------------------------
    # source quality / channel aggregate
    # -----------------------------------------------------
    def _calculate_source_quality(self, evidences: List[EvidenceRecord]) -> float:
        if not evidences:
            return 0.0

        vals = []
        for ev in evidences:
            rule = self.repo.get_source_rule(ev.source_type)
            credibility = float(rule["credibility_weight"])
            directness = float(rule["directness_base_weight"])
            update_rel = float(rule["update_reliability_weight"])

            scope_bonus = 0.0
            if ev.scope == "model":
                scope_bonus += 0.08
            elif ev.scope in {"product", "product_or_model"}:
                scope_bonus += 0.05

            if ev.matched_model:
                scope_bonus += 0.08
            elif ev.matched_product:
                scope_bonus += 0.05

            val = clamp((credibility * 0.45 + directness * 0.35 + update_rel * 0.20 + scope_bonus) * 100.0)
            vals.append(val)

        return round(sum(vals) / len(vals), 2)

    def _aggregate_channel_score(self, caps: List[CapabilityScore], source_pool: set) -> float:
        channel_caps = [c for c in caps if set(c.supporting_sources) & source_pool]
        if not channel_caps:
            return 0.0
        # 상위 capability가 채널 점수에 더 많이 반영되도록 상위 3개 평균
        top = sorted(channel_caps, key=lambda x: x.final_score, reverse=True)[:3]
        return round(sum(c.final_score for c in top) / len(top), 2)

    def _calculate_confidence(
        self,
        caps: List[CapabilityScore],
        evidence_records: List[EvidenceRecord],
        h_found: int,
        t_found: int,
        c_found: int
    ) -> float:
        if not caps:
            return 0.0

        positive_caps = [c for c in caps if c.positive_claim]
        top = sorted(caps, key=lambda x: x.final_score, reverse=True)[:3]

        channel_factor = ((h_found + t_found + c_found) / 3.0) * 35.0
        evidence_factor = min(len(evidence_records) / 12.0, 1.0) * 20.0
        capability_factor = min(len(positive_caps) / 3.0, 1.0) * 15.0
        score_factor = (sum(c.final_score for c in top) / max(1, len(top))) * 0.20
        support_source_factor = min(len(set(s for c in top for s in c.supporting_sources)) / 5.0, 1.0) * 10.0

        conf = clamp(channel_factor + evidence_factor + capability_factor + score_factor + support_source_factor)
        return round(conf, 2)

    # -----------------------------------------------------
    # verdict / reason
    # -----------------------------------------------------
    def _decide_verdict(self, accs: float, conf: float, caps: List[CapabilityScore]) -> Tuple[str, str]:
        positive_top = [c for c in sorted(caps, key=lambda x: x.final_score, reverse=True) if c.positive_claim][:3]

        # capability가 거의 안 잡히면 단순 높은 점수여도 확정하지 않음
        if not positive_top and accs < 70:
            return "불확실", "중간"

        if accs >= 80 and conf >= 65:
            return "신뢰 가능", "낮음"
        if accs >= 60 and conf >= 50:
            return "추가 검토 필요", "중간"
        if accs >= 40:
            return "근거 부족", "중간~높음"
        return "AI Washing 의심", "높음"

    def _build_reasons(
        self,
        accs: float,
        raw_accs: float,
        hes: float,
        tes: float,
        ces: float,
        ecs: float,
        conf: float,
        verdict: str,
        risk_level: str,
        top_caps: List[CapabilityScore],
        used_caps: List[CapabilityScore]
    ) -> List[str]:
        reasons = []
        reasons.append(f"온톨로지 기반 최종 ACCS는 {accs:.1f}점이며, 존재 채널만 반영한 Raw ACCS는 {raw_accs:.1f}점입니다.")
        reasons.append(f"HES {hes:.1f}점, TES {tes:.1f}점, CES {ces:.1f}점, ECS {ecs:.1f}점, CONF {conf:.1f}점으로 계산되었습니다.")

        if top_caps:
            cap_desc = ", ".join([f"{c.capability_name_ko}({c.final_score:.1f})" for c in top_caps[:3]])
            reasons.append(f"가장 강하게 뒷받침된 capability는 {cap_desc} 입니다.")

        missing_heavy = []
        for c in used_caps[:5]:
            if c.missing_required_components and c.positive_claim:
                missing_heavy.append(f"{c.capability_name_ko}: {', '.join(c.missing_required_components[:2])}")
        if missing_heavy:
            reasons.append("주장 기능 대비 일부 필수 requirement가 부족했습니다: " + " / ".join(missing_heavy[:3]))

        reasons.append(f"최종 판정은 '{verdict}', 위험도는 '{risk_level}'입니다.")
        return reasons

    def _count_evidence_by_source(self, evidence_records: List[EvidenceRecord]) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for ev in evidence_records:
            result[ev.source_type] = result.get(ev.source_type, 0) + 1
        return dict(sorted(result.items(), key=lambda x: x[0]))


# =========================================================
# feature_scraper 브랜치용 adapter helper
# =========================================================

def bundle_to_evidence_records(
    product_json: Optional[Dict[str, Any]] = None,
    norm_info: Optional[Dict[str, Any]] = None,
    db_results: Optional[List[Dict[str, Any]]] = None,
    jodale_result: Optional[Any] = None,
    tipa_result: Optional[Any] = None,
    koraia_result: Optional[Any] = None,
    patent_items_df: Optional[Any] = None,
    cert_results: Optional[List[Dict[str, Any]]] = None,
    dart_result: Optional[Dict[str, Any]] = None,
    target_company_name: str = "",
    model_param: str = "",
) -> List[EvidenceRecord]:
    """
    feature_scraper 브랜치의 수집 결과를 분석 엔진 입력으로 변환
    """
    records: List[EvidenceRecord] = []

    # 1) seller page / product text
    if product_json:
        seller_text_parts = []
        for key in ["name", "product_name", "title", "description", "spec_summary", "ocr_text", "refined_text"]:
            val = product_json.get(key)
            if isinstance(val, str) and val.strip():
                seller_text_parts.append(val)

        specs = product_json.get("specs")
        if isinstance(specs, dict):
            for k, v in specs.items():
                seller_text_parts.append(f"{k}: {v}")

        if seller_text_parts:
            text = " ".join(seller_text_parts)
            records.append(EvidenceRecord(
                source_type="seller_page",
                text=text,
                scope="product" if model_param else "product_or_model",
                title=product_json.get("name", "") or product_json.get("product_name", ""),
                matched_company=bool(target_company_name),
                matched_product=True,
                matched_model=bool(model_param),
            ))

    # 2) KC / RRA DB results
    for row in (db_results or []):
        source_type = "kc"
        title = str(row.get("product_name") or row.get("title") or "KC/RRA DB")
        text = " ".join([str(v) for v in row.values() if v is not None])
        model_match = model_param and model_param.lower() in text.lower()
        company_match = target_company_name and target_company_name.lower() in text.lower()
        records.append(EvidenceRecord(
            source_type=source_type,
            text=text,
            scope="model" if model_match else "product",
            title=title,
            matched_company=bool(company_match),
            matched_product=True,
            matched_model=bool(model_match),
            matched_components=["카메라 센서"] if any(x in text.lower() for x in ["카메라", "camera", "영상"]) else [],
        ))

    # 3) procurement / 조달청
    if jodale_result:
        txt = str(jodale_result)
        records.append(EvidenceRecord(
            source_type="procurement",
            text=txt,
            scope="product" if model_param else "company",
            title="조달청 결과",
            matched_company=bool(target_company_name and target_company_name.lower() in txt.lower()),
            matched_product=True,
            matched_model=bool(model_param and model_param.lower() in txt.lower()),
        ))

    # 4) TIPA
    if tipa_result:
        txt = str(tipa_result)
        records.append(EvidenceRecord(
            source_type="tipa",
            text=txt,
            scope="company",
            title="TIPA 결과",
            matched_company=bool(target_company_name and target_company_name.lower() in txt.lower()),
        ))

    # 5) KORAIA
    if koraia_result:
        txt = str(koraia_result)
        records.append(EvidenceRecord(
            source_type="koraia",
            text=txt,
            scope="company",
            title="KORAIA 결과",
            matched_company=bool(target_company_name and target_company_name.lower() in txt.lower()),
        ))

    # 6) 특허
    if patent_items_df is not None:
        try:
            patent_rows = patent_items_df.to_dict(orient="records")
        except Exception:
            patent_rows = []

        for row in patent_rows:
            txt = " ".join([str(v) for v in row.values() if v is not None])
            records.append(EvidenceRecord(
                source_type="kipris",
                text=txt,
                scope="company",
                title=str(row.get("발명의명칭") or row.get("title") or "특허"),
                matched_company=bool(target_company_name and target_company_name.lower() in txt.lower()),
                matched_model=bool(model_param and model_param.lower() in txt.lower()),
            ))

    # 7) 인증
    for row in (cert_results or []):
        txt = " ".join([str(v) for v in row.values() if v is not None])
        cert_name = normalize_text(txt)
        if "gs" in cert_name:
            source = "gs"
        elif "nep" in cert_name:
            source = "nep"
        else:
            source = "gs"
        records.append(EvidenceRecord(
            source_type=source,
            text=txt,
            scope="product",
            title=str(row.get("name") or row.get("title") or "인증"),
            matched_company=bool(target_company_name and target_company_name.lower() in txt.lower()),
            matched_product=True,
            matched_model=bool(model_param and model_param.lower() in txt.lower()),
        ))

    # 8) DART
    if dart_result:
        txt = str(dart_result)
        records.append(EvidenceRecord(
            source_type="dart",
            text=txt,
            scope="company",
            title="DART 공시",
            matched_company=True if target_company_name else False,
        ))

    return records


def analyze_feature_scraper_bundle(
    ontology_dir: str,
    product_json: Optional[Dict[str, Any]] = None,
    norm_info: Optional[Dict[str, Any]] = None,
    db_results: Optional[List[Dict[str, Any]]] = None,
    jodale_result: Optional[Any] = None,
    tipa_result: Optional[Any] = None,
    koraia_result: Optional[Any] = None,
    patent_items_df: Optional[Any] = None,
    cert_results: Optional[List[Dict[str, Any]]] = None,
    dart_result: Optional[Dict[str, Any]] = None,
    target_company_name: str = "",
    model_param: str = "",
) -> AnalysisResult:
    records = bundle_to_evidence_records(
        product_json=product_json,
        norm_info=norm_info,
        db_results=db_results,
        jodale_result=jodale_result,
        tipa_result=tipa_result,
        koraia_result=koraia_result,
        patent_items_df=patent_items_df,
        cert_results=cert_results,
        dart_result=dart_result,
        target_company_name=target_company_name,
        model_param=model_param,
    )

    ad_text = ""
    ocr_text = ""
    if product_json:
        ad_text = str(product_json.get("description") or product_json.get("name") or product_json.get("product_name") or "")
        ocr_text = str(product_json.get("ocr_text") or product_json.get("refined_text") or "")

    engine = OntologyAnalysisEngine(ontology_dir=ontology_dir)
    return engine.analyze(records, ad_text=ad_text, ocr_text=ocr_text)


# =========================================================
# server.py 연동 예시
# =========================================================

SERVER_INTEGRATION_EXAMPLE = r"""
# server.py 예시

from ontology_analysis_engine_v2 import analyze_feature_scraper_bundle

analysis_result = analyze_feature_scraper_bundle(
    ontology_dir=os.path.dirname(os.path.abspath(__file__)),
    product_json=product_json,
    norm_info=norm_info,
    db_results=db_results,
    jodale_result=jodale_result,
    tipa_result=tipa_result,
    koraia_result=koraia_result,
    patent_items_df=patent_items_df,
    cert_results=cert_results,
    dart_result=dart_result,
    target_company_name=target_company_name,
    model_param=model_param,
)

result = {
    "scores": {
        "ACCS": analysis_result.accs,
        "RawACCS": analysis_result.raw_accs,
        "HES": analysis_result.hes,
        "TES": analysis_result.tes,
        "CES": analysis_result.ces,
        "ECS": analysis_result.ecs,
        "CONF": analysis_result.conf,
    },
    "verdict": analysis_result.verdict,
    "risk_level": analysis_result.risk_level,
    "reasons": analysis_result.reasons,
    "top_capabilities": analysis_result.top_capabilities,
    "capability_scores": analysis_result.capability_scores,
    "details": analysis_result.details,
}
"""
