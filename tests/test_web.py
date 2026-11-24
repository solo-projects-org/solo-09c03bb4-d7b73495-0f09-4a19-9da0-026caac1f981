"""网页端到端测试：通过 HTTP 上传两期持仓与公告、查看净变动与失败提示。"""

import io

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "web.db"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        }
    )
    return app.test_client()


BEGIN = "证券代码,股数\n000001,100\n000002,200\n".encode("utf-8")
END = "证券代码,股数\n000001X,300\n000002,190\n".encode("utf-8")
ANN = "生效日,旧代码,新代码,新旧股数比\n2026-01-15,000001,000001X,3\n".encode("utf-8")


def _upload(client, path, date, data):
    return client.post(
        path,
        data={
            "as_of_date": date,
            "file": (io.BytesIO(data), "x.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def test_index_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "净变动".encode() in resp.data


def test_upload_holdings_and_announcements_renders_net_change(client):
    resp = _upload(client, "/holdings/begin", "2026-01-01", BEGIN)
    assert resp.status_code == 200
    resp = client.post(
        "/announcements",
        data={"file": (io.BytesIO(ANN), "ann.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "整表换报成功".encode() in resp.data
    resp = _upload(client, "/holdings/end", "2026-01-31", END)
    text = resp.data.decode()
    # 000001X：折算 300、实报 300、净变动 0；000002：200 vs 190，净变动 -10
    assert "000001X" in text
    assert "000002" in text
    assert "-10" in text


def test_bad_announcement_flashes_and_keeps_old(client):
    _upload(client, "/holdings/begin", "2026-01-01", BEGIN)
    client.post(
        "/announcements",
        data={"file": (io.BytesIO(ANN), "ann.csv")},
        content_type="multipart/form-data",
    )
    bad = (
        "生效日,旧代码,新代码,新旧股数比\n"
        "2026-01-15,000001,000001X,3\n"
        "2026-01-15,000001,000001Y,2\n"
    ).encode("utf-8")
    resp = client.post(
        "/announcements",
        data={"file": (io.BytesIO(bad), "bad.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    text = resp.data.decode()
    assert "公告换报失败" in text
    assert "failed" in text
    # 原结果仍在页面上
    assert "000001X" in text


def test_missing_file_returns_flash_not_500(client):
    resp = client.post(
        "/announcements",
        data={},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "公告换报失败" in resp.data.decode()
