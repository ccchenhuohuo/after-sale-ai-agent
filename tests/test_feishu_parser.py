from agent_runtime.feishu.parser import extract_text_content, should_trigger_ai, strip_trigger_prefix


def test_extract_text_content_from_feishu_text_json():
    assert extract_text_content('{"text": "AI分析：A100 绑定不上"}') == "AI分析：A100 绑定不上"


def test_should_trigger_by_prefix():
    assert should_trigger_ai("AI分析：A100 绑定不上")
    assert not should_trigger_ai("普通群聊消息")


def test_strip_trigger_prefix():
    assert strip_trigger_prefix("AI分析：A100 绑定不上") == "A100 绑定不上"
