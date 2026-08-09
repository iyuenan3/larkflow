"""Hosted web search extraction and citation safety."""
from __future__ import annotations

import pytest

from larkflow.llm import LLMUnavailable, OpenAICompatLLM


ROLES = {
    "default": {
        "base_url": "https://ark.example.invalid/api/v3",
        "api_key": "test-key",
        "model": "test-model",
    }
}


class FakeResponsesClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    @property
    def responses(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def response(*, annotations):
    return {
        "output": [
            {"type": "web_search_call", "status": "completed"},
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "苏州博物馆需要按官方规则预约。",
                        "annotations": annotations,
                    }
                ],
            },
        ]
    }


def test_hosted_web_search_extracts_text_and_deduplicated_citations():
    fake = FakeResponsesClient(
        response(
            annotations=[
                {
                    "type": "url_citation",
                    "url": "https://www.szmuseum.com/visit",
                    "title": "参观须知",
                },
                {
                    "type": "url_citation",
                    "url": "https://www.szmuseum.com/visit",
                    "title": "参观须知",
                },
            ]
        )
    )
    llm = OpenAICompatLLM(ROLES, client_factory=lambda _cfg: fake)

    result = llm.web_search(prompt="核对苏州景点规则", model_role="default")

    assert result == {
        "content": "苏州博物馆需要按官方规则预约。",
        "sources": ["https://www.szmuseum.com/visit"],
    }
    assert fake.calls == [
        {
            "model": "test-model",
            "input": "核对苏州景点规则",
            "tools": [{"type": "web_search"}],
        }
    ]


def test_hosted_web_search_refuses_an_unsourced_answer():
    fake = FakeResponsesClient(response(annotations=[]))
    llm = OpenAICompatLLM(ROLES, client_factory=lambda _cfg: fake)

    with pytest.raises(LLMUnavailable, match="no cited sources"):
        llm.web_search(prompt="核对苏州景点规则", model_role="default")


def test_visible_url_without_provider_citation_is_not_trusted():
    value = response(annotations=[])
    value["output"][1]["content"][0]["text"] = (
        "官方来源：https://www.szmuseum.com/）「参观服务」栏目"
    )
    fake = FakeResponsesClient(value)
    llm = OpenAICompatLLM(ROLES, client_factory=lambda _cfg: fake)

    with pytest.raises(LLMUnavailable, match="no cited sources"):
        llm.web_search(prompt="核对苏州景点规则", model_role="default")
