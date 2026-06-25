from agent_runtime.settings import Settings
from agent_runtime.tools.sku_catalog import resolve_sku_evidence


def _settings_with_catalog(tmp_path):
    path = tmp_path / "sku.csv"
    path.write_text(
        "\n".join(
            [
                "sku_code,spu,sku_name_cn,product_name_cn,product_owner_name",
                "L023,L023,L023 直播补光灯,直播补光灯,售后负责人A",
                "1609,1609,ULANZI 52MM变形电影镜头镜头转接滤镜环,ULANZI 52MM变形电影镜头镜头转接滤镜环,售后负责人C",
                "I001,S,SnapPod 自拍屏,SnapPod 自拍屏,文韬",
                "T220,GP02,1,GP02 JOBY GorillaPod 1K,陈志强",
            ]
        ),
        encoding="utf-8",
    )
    return Settings(sku_catalog_path=str(path))


def test_resolve_sku_evidence_hits_meaningful_sku_code(tmp_path):
    items = resolve_sku_evidence("L023 客户反馈充电2小时仍无法开机。", settings=_settings_with_catalog(tmp_path))

    assert items[0].status == "hit"
    assert items[0].sku == "L023"


def test_resolve_sku_evidence_ignores_generic_sku_word_and_short_catalog_fields(tmp_path):
    items = resolve_sku_evidence(
        "客户反馈未知型号 ZQ-NOSKU-8842，需补充订单SKU、包装SKU或铭牌。",
        settings=_settings_with_catalog(tmp_path),
    )

    assert len(items) == 1
    assert items[0].status == "empty"


def test_resolve_sku_evidence_ignores_numeric_sku_inside_timestamp(tmp_path):
    items = resolve_sku_evidence(
        "HITL全未命中最终复测 20260618-1609：客户反馈未知设备代号 RNDX-8842。",
        settings=_settings_with_catalog(tmp_path),
    )

    assert len(items) == 1
    assert items[0].status == "empty"


def test_resolve_sku_evidence_hits_standalone_numeric_sku_token(tmp_path):
    items = resolve_sku_evidence("1609 镜头转接环无法安装。", settings=_settings_with_catalog(tmp_path))

    assert items[0].status == "hit"
    assert items[0].sku == "1609"
