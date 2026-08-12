"""公式 keirin.jp のバンク諸元パースのテスト。

ページ内に「エスケープされたJSON文字列」として埋まっているので、
外側のJSONをパースしても取れない。必要なキーだけ正規表現で拾っている。
脆い作りなので、取れなかったときに黙って0や誤値にならないことを担保する。
"""

import pytest

from keirin.keirinjp import _dms_to_deg, parse_velodrome

# 実ページから必要部分だけ抜き出したもの (弥彦, jocd=21)
SAMPLE = (
    '{"jyoCode":"21","jyoName":"弥彦競輪場",'
    '"syutyoImg":"/pc/static/img/sisetsu/bank400.gif",'
    '"houiImg":"/pc/static/img/sisetsu/compass90.gif",'
    '"tyokusen":"63.1m","kant":"32&deg;24&prime;17&Prime;",'
    '"tkant":"2&deg;51&prime;45&Prime;",'
    '"homeHukuin":"10.1m","backHukuin":"9.0m","centerHukuin":"7.3m",'
    '"maxAgari":"10.6秒","maxAgariMei":"山崎　芳仁",'
    '"firstTechniqCnt":"632","firstTechniqList":['
    '{"iconName":"逃げ","percentName":"19%"},'
    '{"iconName":"捲り","percentName":"30%"},'
    '{"iconName":"差し","percentName":"51%"}],'
    '"secondTechniqCnt":"628","secondTechniqList":['
    '{"iconName":"逃げ","percentName":"22%"},'
    '{"iconName":"捲り","percentName":"14%"},'
    '{"iconName":"差し","percentName":"25%"},'
    '{"iconName":"マーク","percentName":"39%"}]}'
)


class TestParseVelodrome:
    def setup_method(self):
        self.d = parse_velodrome(SAMPLE)

    def test_identity(self):
        assert self.d["jyo_cd"] == "21"
        assert self.d["jyo_name"] == "弥彦競輪場"

    def test_bank_length_comes_from_the_diagram_filename(self):
        """周長は数値では持っておらず bank400.gif という画像名にしかない。"""
        assert self.d["bank_length_m"] == 400.0

    def test_compass_bearing(self):
        assert self.d["compass_deg"] == 90.0

    def test_straight_and_widths(self):
        assert self.d["straight_m"] == 63.1
        assert self.d["home_width_m"] == 10.1
        assert self.d["back_width_m"] == 9.0
        assert self.d["center_width_m"] == 7.3

    def test_cant_is_converted_from_dms(self):
        assert self.d["bank_angle_deg"] == pytest.approx(32.4047, abs=1e-3)
        assert self.d["straight_angle_deg"] == pytest.approx(2.8625, abs=1e-3)

    def test_max_agari(self):
        assert self.d["max_agari_sec"] == 10.6

    def test_first_place_shares_only(self):
        """1着グラフと2着グラフが同じキー名で並ぶ。1着だけを採ること。"""
        assert self.d["share_nige"] == 0.19
        assert self.d["share_makuri"] == 0.30
        assert self.d["share_sashi"] == 0.51

    def test_first_place_shares_sum_to_one(self):
        """1着の決まり手は逃げ・捲り・差しの3種。マークを混ぜると1を超える。"""
        total = self.d["share_nige"] + self.d["share_makuri"] + self.d["share_sashi"]
        assert total == pytest.approx(1.0)

    def test_mark_is_not_treated_as_a_first_place_move(self):
        assert "share_mark" not in self.d

    def test_missing_fields_become_none_not_zero(self):
        """取れなかった項目が0になると、平坦なバンクと区別がつかなくなる。"""
        d = parse_velodrome('{"jyoCode":"99","jyoName":"テスト"}')
        assert d["bank_length_m"] is None
        assert d["straight_m"] is None
        assert d["share_nige"] is None

    def test_garbage_input_does_not_raise(self):
        d = parse_velodrome("<html>not json at all</html>")
        assert d["jyo_cd"] is None


class TestDmsConversion:
    def test_degrees_minutes_seconds(self):
        assert _dms_to_deg("32&deg;24&prime;17&Prime;") == pytest.approx(32.4047, abs=1e-3)

    def test_plain_degree_sign(self):
        assert _dms_to_deg("25°30′0″") == pytest.approx(25.5)

    def test_degrees_only(self):
        assert _dms_to_deg("30°") == pytest.approx(30.0)

    def test_none_and_garbage(self):
        assert _dms_to_deg(None) is None
        assert _dms_to_deg("") is None
        assert _dms_to_deg("abc") is None
