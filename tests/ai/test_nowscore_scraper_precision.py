from app.services.nowscore_scraper import _parse_h2h, _parse_odds_trend_tables, _parse_recent_form


def test_result_table_with_size_column_is_not_initial_odds():
    html = """
    <table>
      <tr><th>日期</th><th>主场</th><th>比分</th><th>客场</th><th>让球</th><th>大小</th></tr>
      <tr><td>2026-08-01</td><td>A</td><td>2-1</td><td>B</td><td>赢</td><td>大</td></tr>
    </table>
    """

    parsed = _parse_odds_trend_tables(html)

    assert parsed["initial_odds"] == []
    assert parsed["market_odds"]["available"] is False


def test_performance_table_is_kept_but_not_labeled_as_market_odds():
    html = """
    <h2>盘路走势</h2>
    <table>
      <tr><th>全场</th><th>赢</th><th>走</th><th>输</th><th>赢%</th><th>大球</th><th>走</th><th>小球</th><th>大%</th></tr>
      <tr><td>A</td><td>3</td><td>0</td><td>2</td><td>60%</td><td>2</td><td>0</td><td>3</td><td>40%</td></tr>
    </table>
    """

    parsed = _parse_odds_trend_tables(html)

    assert parsed["performance"]["available"] is True
    assert parsed["tables"]
    assert parsed["market_odds"]["available"] is False
    assert parsed["initial_odds"] == []


def test_verified_company_opening_and_live_table_is_market_odds():
    html = """
    <table>
      <tr><th>公司</th><th>初盘大小</th><th>初盘赔率</th><th>即时大小</th><th>即时赔率</th></tr>
      <tr><td>Bet365</td><td>2.5</td><td>0.90</td><td>2.75</td><td>0.94</td></tr>
    </table>
    """

    parsed = _parse_odds_trend_tables(html)

    assert parsed["market_odds"]["available"] is True
    assert len(parsed["initial_odds"]) == 1
    assert parsed["performance"]["available"] is False


def test_recent_form_is_selected_by_team_not_fixed_table_index():
    # H2H 只有 1 场时表格不足 4 行，不会进入 form_tables；旧代码因此主客表整体错一位。
    html = """
    <table>
      <tr><th>日期</th><th>赛事</th><th>主场</th><th>比分</th><th>客场</th></tr>
      <tr><td>25-08-17</td><td>友谊赛</td><td>主队甲</td><td>65-77</td><td>客队乙</td></tr>
    </table>
    <table>
      <tr><th>日期</th><th>赛事</th><th>主场</th><th>比分</th><th>客场</th></tr>
      <tr><td>26-08-24</td><td>友谊赛</td><td>主队甲</td><td>65-89</td><td>球队丙</td></tr>
      <tr><td>26-08-08</td><td>友谊赛</td><td>主队甲</td><td>64-78</td><td>球队丁</td></tr>
      <tr><td>26-08-01</td><td>友谊赛</td><td>球队戊</td><td>80-70</td><td>主队甲</td></tr>
    </table>
    <table>
      <tr><th>日期</th><th>赛事</th><th>主场</th><th>比分</th><th>客场</th></tr>
      <tr><td>26-08-24</td><td>友谊赛</td><td>客队乙</td><td>79-95</td><td>球队己</td></tr>
      <tr><td>26-08-03</td><td>友谊赛</td><td>球队庚</td><td>71-113</td><td>客队乙</td></tr>
      <tr><td>26-08-02</td><td>友谊赛</td><td>客队乙</td><td>106-70</td><td>球队辛</td></tr>
    </table>
    """

    home = _parse_recent_form(html, "home", team_name="主队甲")
    away = _parse_recent_form(html, "away", team_name="客队乙")

    assert len(home["matches"]) == 3
    assert len(away["matches"]) == 3
    assert all("主队甲" in (row["home"], row["away"]) for row in home["matches"])
    assert all("客队乙" in (row["home"], row["away"]) for row in away["matches"])

    h2h = _parse_h2h(html, "主队甲", "客队乙")
    assert len(h2h["matches"]) == 1
