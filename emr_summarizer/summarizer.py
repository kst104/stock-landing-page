"""
Claude API를 사용한 EMR 챠트 자동 요약
"""

from __future__ import annotations

import json

import anthropic

_SYSTEM_PROMPT = """당신은 경험 많은 한국 의사를 보조하는 임상 AI 어시스턴트입니다.
환자의 EMR 챠트 데이터를 받아 임상적으로 유용한 요약을 한국어로 작성합니다.

요약 원칙:
- 의학적으로 중요한 정보를 우선 기술
- 간결하고 명확한 의학 용어 사용
- 수치 결과는 정상/비정상 여부 명시
- 불필요한 반복 생략
- 섹션별 구조화된 형식 사용"""

_USER_TEMPLATE = """다음 환자의 EMR 챠트를 요약해주세요.

=== 환자 데이터 ===
{chart_json}

=== 요약 형식 ===
## 환자 기본 정보
(나이, 성별, 간단한 신원 정보)

## 주요 진단
(현재 및 최근 진단명, ICD 코드 포함 시 병기)

## 현재 약물 처방
(최근 처방된 주요 약물, 용량, 복용 횟수)

## 최근 검사 결과 요약
(이상 소견 위주, 정상 항목은 "기타 항목 정상 범위" 로 일괄 처리)

## 최근 내원 경과
(최근 2–3회 내원 요약, 주호소 및 경과)

## 임상적 주의사항
(알레르기, 금기 약물, 중요 병력 등 주의가 필요한 항목)"""


def summarize(chart_data: dict, api_key: str, model: str) -> str:
    """
    chart_data: get_all_chart_data() 반환값
    반환: 요약 텍스트 (Markdown)
    """
    client = anthropic.Anthropic(api_key=api_key)

    chart_json = json.dumps(chart_data, ensure_ascii=False, indent=2)
    user_msg = _USER_TEMPLATE.format(chart_json=chart_json)

    message = client.messages.create(
        model=model,
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return message.content[0].text
