"""
Claude Vision API로 여러 EMR 화면을 종합 분석 및 요약
"""

from __future__ import annotations

import base64
import io

import anthropic
from PIL import Image

_SYSTEM_PROMPT = """당신은 경험 많은 한국 의사를 보조하는 임상 AI 어시스턴트입니다.
EMR 챠트의 여러 화면을 순서대로 받아 환자의 전체 이력을 종합 분석하고
임상적으로 유용한 요약을 한국어로 작성합니다.

요약 원칙:
- 모든 화면에 걸쳐 의학적으로 중요한 정보를 통합
- 간결하고 명확한 의학 용어 사용
- 수치는 정상/비정상 여부 명시
- 시간 순서가 있으면 최근 → 과거 순으로 기술
- 섹션별 구조화된 형식 사용"""

_PROMPT = """아래 이미지들은 같은 환자의 EMR 챠트 여러 화면입니다.
모든 화면의 내용을 종합하여 다음 형식으로 요약해주세요.

## 환자 기본 정보
(나이, 성별, 등록번호 등)

## 주요 진단 및 병력
(현재 및 과거 진단명, 주요 병력)

## 현재 복용 약물
(최근 처방된 약물, 용량, 복용 횟수)

## 검사 결과 요약
(이상 소견 위주, 추세 포함, 정상 항목은 "나머지 정상"으로 처리)

## 내원 경과 요약
(최근 내원 주호소 및 경과)

## 임상적 주의사항
(알레르기, 금기약물, 중요 병력)

## 종합 소견
(전체 이력 핵심 요약 2-3줄)

화면에 없는 항목은 "(정보 없음)"으로 표시하세요."""


_BRIEF_PROMPT = """아래 이미지들은 같은 환자의 EMR 챠트 여러 화면입니다.
모든 화면을 종합하여 **A4 용지 2/3 분량(약 350~450단어)**의 '초간단 요약본'을 작성하세요.

작성 규칙:
- 의사가 30초 안에 환자를 파악할 수 있도록 핵심만
- 섹션 제목 없이 4~6개의 짧은 단락 또는 불릿으로 구성
- 다음 순서로 핵심만 포함:
  ① 환자(나이/성별), 주진단 ② 주요 병력·현재 약물 핵심
  ③ 꼭 알아야 할 이상 검사 소견(추세 포함) ④ 최근 내원 경과
  ⑤ 임상적 주의사항(알레르기·금기·중요 병력)
- 정상이거나 사소한 내용은 과감히 생략
- 분량이 A4 2/3를 넘지 않도록 압축
- 화면에 없으면 그 항목은 아예 언급하지 않음"""


def _resize(image: Image.Image, max_width: int = 1280) -> Image.Image:
    if image.width > max_width:
        ratio = max_width / image.width
        return image.resize((max_width, int(image.height * ratio)), Image.LANCZOS)
    return image


def _to_b64(image: Image.Image) -> str:
    buf = io.BytesIO()
    _resize(image).save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def summarize_images(
    items: list[tuple[str, Image.Image]] | list[Image.Image],
    api_key: str,
    model: str,
    brief: bool = False,
) -> str:
    """
    [(탭명, Image), ...] 또는 [Image, ...] → 종합 요약 텍스트

    brief=False : 섹션별 상세 종합 요약 (기본)
    brief=True  : A4 2/3 분량 '초간단 요약본'
    """
    if not items:
        raise ValueError("캡처된 이미지가 없습니다.")

    # 형식 통일: (label, image) 튜플로
    pairs: list[tuple[str, Image.Image]] = []
    for i, item in enumerate(items, 1):
        if isinstance(item, tuple):
            pairs.append(item)
        else:
            pairs.append((f"화면 {i}", item))

    content: list[dict] = []
    for label, img in pairs:
        content.append({"type": "text", "text": f"[{label}]"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": _to_b64(img),
            },
        })
    content.append({"type": "text",
                    "text": _BRIEF_PROMPT if brief else _PROMPT})

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=1200 if brief else 3000,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return message.content[0].text
