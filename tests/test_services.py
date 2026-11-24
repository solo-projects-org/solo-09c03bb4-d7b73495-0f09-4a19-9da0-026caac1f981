"""服务层测试：净变动（缺项按零）、公告整表换报的增量重算与失败可查。"""

import io

import pytest

from app import create_app
from app.csv_utils import CsvError
from app.db import get_db
from app.services import get_net_change, replace_announcements, upload_holding


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.db"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        }
    )
    yield app


def csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


BEGIN_CSV = csv_bytes(
    "证券代码,股数\n"
    "000001,100\n"   # 将被拆股/换股
    "000002,200\n"   # 公告不涉及
    "000003,50\n"    # 只在期末出现实报变化
)

END_CSV = csv_bytes(
    "证券代码,股数\n"
    "000001X,300\n"  # 000001 一拆三后的实报
    "000002,200\n"
    "000003,55\n"    # 净买入 5
    "000009,8\n"     # 折算侧缺项，按零
)

ANN_V1 = csv_bytes(
    "生效日,旧代码,新代码,新旧股数比\n"
    "2026-01-15,000001,000001X,3\n"
)

# V2：只改 000001 的比例；000002 的公告是新增的另一条
ANN_V2 = csv_bytes(
    "生效日,旧代码,新代码,新旧股数比\n"
    "2026-01-15,000001,000001X,4\n"
    "2026-01-20,000002,000002X,1\n"
)


def _net_map(rows):
    return {
        r.end_code: (r.adjusted_shares, r.reported_shares, r.net_change)
        for r in rows
    }


def test_full_flow_net_change_and_missing_sides(app):
    with app.app_context():
        upload_holding("begin", "2026-01-01", "begin.csv", BEGIN_CSV)
        r1, rows1 = get_net_change()
        # 期末未上传：实报全为零，折算按无公告原样
        m1 = _net_map(rows1)
        assert m1["000001"] == (100, 0, -100)
        assert m1["000003"] == (50, 0, -50)

        replace_announcements("ann_v1.csv", ANN_V1)
        upload_holding("end", "2026-01-31", "end.csv", END_CSV)
        _r, rows = get_net_change()
        m = _net_map(rows)
        assert m["000001X"] == (300, 300, 0)       # 100 * 3，实报一致
        assert m["000002"] == (200, 200, 0)
        assert m["000003"] == (50, 55, 5)         # 净买入
        assert m["000009"] == (0, 8, 8)           # 折算侧缺项按零
        # 原代码 000001 折算后已不存在，不应再单列
        assert "000001" not in m


def test_announcement_replace_only_recomputes_affected(app):
    with app.app_context():
        upload_holding("begin", "2026-01-01", "begin.csv", BEGIN_CSV)
        replace_announcements("ann_v1.csv", ANN_V1)
        r_before, _ = get_net_change()
        before = {
            row["origin_code"]: row
            for row in get_db().execute(
                "SELECT * FROM result_lot WHERE result_batch_id=?",
                (r_before["id"],),
            ).fetchall()
        }

        msg, new_result_id = replace_announcements("ann_v2.csv", ANN_V2)
        assert new_result_id is not None
        # 000001、000002 受影响需重算，000003 沿用上一结果批次的行
        assert "受影响期初持仓 2" in msg
        assert "1 条沿用" in msg

        new_lots = {
            row["origin_code"]: row
            for row in get_db().execute(
                "SELECT * FROM result_lot WHERE result_batch_id=?",
                (new_result_id,),
            ).fetchall()
        }
        # 000001 按新比例 4 重算
        assert new_lots["000001"]["end_code"] == "000001X"
        assert new_lots["000001"]["adjusted_shares"] == "400"
        # 000002 也受影响
        assert new_lots["000002"]["end_code"] == "000002X"
        # 000003 未受影响，沿用原行（内容逐字一致）
        assert (
            new_lots["000003"]["adjusted_shares"]
            == before["000003"]["adjusted_shares"]
        )
        assert new_lots["000003"]["path_json"] == before["000003"]["path_json"]

        # 旧批次变为 superseded，仍可按旧结果批次查询
        statuses = {
            r["status"]
            for r in get_db()
            .execute("SELECT status FROM announcement_batch")
            .fetchall()
        }
        assert "active" in statuses and "superseded" in statuses
        old_result, old_rows = get_net_change(r_before["id"])
        assert old_result is not None
        assert _net_map(old_rows)["000001X"] == (300, 0, -300)


def test_failed_replace_keeps_original_table(app):
    with app.app_context():
        upload_holding("begin", "2026-01-01", "begin.csv", BEGIN_CSV)
        replace_announcements("ann_v1.csv", ANN_V1)
        good, _ = get_net_change()

        bad_csv = csv_bytes(
            "生效日,旧代码,新代码,新旧股数比\n"
            "2026-01-15,000001,000001X,3\n"
            "2026-01-15,000001,000001Y,2\n"  # 同日同旧代码两条
        )
        with pytest.raises(CsvError):
            replace_announcements("bad.csv", bad_csv)

        # 原 active 表与原结果仍可正常查询（此时期末未上传，折算结果为 V1 下的 000001X）
        result, rows = get_net_change()
        assert result["id"] == good["id"]
        m = _net_map(rows)
        assert m["000001X"][0] == 300
        assert "000001" not in m

        statuses = get_db().execute(
            "SELECT status, note FROM announcement_batch ORDER BY id"
        ).fetchall()
        assert statuses[-1]["status"] == "failed"
        assert "换报失败" in statuses[-1]["note"]
        active = [s for s in statuses if s["status"] == "active"]
        assert len(active) == 1


def test_identical_reupload_is_noop(app):
    with app.app_context():
        upload_holding("begin", "2026-01-01", "begin.csv", BEGIN_CSV)
        replace_announcements("ann_v1.csv", ANN_V1)
        msg, result_id = replace_announcements("ann_v1.csv", ANN_V1)
        assert result_id is None
        assert "完全一致" in msg


def test_duplicate_holding_code_rejected(app):
    with app.app_context():
        bad = csv_bytes("证券代码,股数\n000001,10\n000001,20\n")
        with pytest.raises(CsvError):
            upload_holding("begin", "2026-01-01", "bad.csv", bad)
        assert get_db().execute("SELECT COUNT(*) c FROM holding_file").fetchone()["c"] == 0


def test_replace_after_begin_file_change_recomputes_all(app):
    """期初文件整表替换后，最新结果批次基于新文件；换公告时未受影响来源
    沿用的是新文件自己的折算行（99 股），绝不会串到旧文件的 50 股。"""
    with app.app_context():
        upload_holding("begin", "2026-01-01", "begin.csv", BEGIN_CSV)
        replace_announcements("ann_v1.csv", ANN_V1)

        # 换一份期初文件（000003 份额变化），触发全量重算
        new_begin = csv_bytes(
            "证券代码,股数\n"
            "000001,100\n"
            "000002,200\n"
            "000003,99\n"
        )
        upload_holding("begin", "2026-01-01", "begin2.csv", new_begin)

        msg, new_result_id = replace_announcements("ann_v2.csv", ANN_V2)
        assert new_result_id is not None
        row_003 = get_db().execute(
            "SELECT adjusted_shares FROM result_lot "
            "WHERE result_batch_id=? AND origin_code='000003'",
            (new_result_id,),
        ).fetchone()
        # 000003 不受公告改动影响，沿用新文件全量重算批次中的 99，而非旧值 50
        assert row_003["adjusted_shares"] == "99"
        assert "1 条沿用" in msg

        # 防护条件：结果批次始终与其引用的期初文件一致，无跨文件复用
        bad = get_db().execute(
            "SELECT COUNT(*) c FROM result_batch rb "
            "JOIN result_lot rl ON rl.result_batch_id=rb.id "
            "JOIN holding_row hr ON hr.file_id=rb.begin_file_id "
            "WHERE rl.origin_code=hr.code AND rl.adjusted_shares='50' "
            "AND rb.begin_file_id=(SELECT MAX(id) FROM holding_file WHERE period='begin')"
        ).fetchone()
        assert bad["c"] == 0


def test_begin_file_mismatch_guard_forces_full_recompute(app, monkeypatch):
    """直接构造“最新结果批次引用旧期初文件”的错位状态，
    验证换报防护会把全部来源纳入重算而非复用旧行。"""
    with app.app_context():
        upload_holding("begin", "2026-01-01", "begin.csv", BEGIN_CSV)
        replace_announcements("ann_v1.csv", ANN_V1)
        conn = get_db()
        old_begin = conn.execute(
            "SELECT id FROM holding_file WHERE period='begin'"
        ).fetchone()["id"]

        # 新期初文件入库并把最新结果批次人为留在旧文件引用上
        new_begin = csv_bytes(
            "证券代码,股数\n000001,100\n000002,200\n000003,99\n"
        )
        upload_holding("begin", "2026-01-02", "begin2.csv", new_begin)
        conn.execute(
            "UPDATE result_batch SET begin_file_id=? WHERE id=(SELECT MAX(id) FROM result_batch)",
            (old_begin,),
        )
        conn.commit()

        msg, new_result_id = replace_announcements("ann_v2.csv", ANN_V2)
        assert "受影响期初持仓 3" in msg and "0 条沿用" in msg
        row_003 = conn.execute(
            "SELECT adjusted_shares FROM result_lot "
            "WHERE result_batch_id=? AND origin_code='000003'",
            (new_result_id,),
        ).fetchone()
        assert row_003["adjusted_shares"] == "99"
