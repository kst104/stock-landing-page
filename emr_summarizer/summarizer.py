"""
Claude Vision API로 EMR 챠트 화면 분석 및 요약
"""

from __future__ import annotations

import base64
import io

import anthropic
from PIL import Image

_SYSTEM_PROMPT = """당신은 경험 많은 한국 의사를 보조하는 임상 AI 어시스턴트입니다.
EMR 챠트 화면 이미지를 보고 임상적으로 유용한 요약을 한국어로 작성합니다.

요약 원칙:
- 화면에 보이는 의학적으로 중요한 정보를 우선 기술
- 간결하고 명확한 의학 용어 사용
- 수치 결과는 정상/비정상 여부 명시
- 섹션별 구조화된 형식 사용"""

_PROMPT = """이 EMR 챠트 화면을 보고 다음 형식으로 요약해주세요.

## 환자 기본 정보
(화면에 보이는 나이, 성별 등)

## 주요 진단
(진단명, 상병코드 등)

## 현재 약물 처방
(최근 처방된 주요 약물, 용량)

## 최근 검사 결과
(이상 소견 위주로, 정상은 "나머지 정상"으로 처리)

## 내원 경과 및 주호소
(최근 내원 내용 요약)

## 임상적 주의사항
(알레르기, 중요 병력 등)

화면에 없는 항목은 "(정보 없음)"으로 표시하세요."""


def summarize_image(image: Image.Image, api_key: str, model: str) -> str:
    """PIL Image → Claude Vision → 요약 텍스트"""
    # PNG로 변환 후 base64 인코딩
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": _PROMPT,
                },
            ],
        }],
    )
    return message.content[0].text
