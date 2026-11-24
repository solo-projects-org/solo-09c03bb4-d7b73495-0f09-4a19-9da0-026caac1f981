"""网页路由：持仓上传、公告换报、净变动查询、历史版本。"""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge

from .csv_utils import CsvError, parse_form_date
from .services import (
    canon,
    get_net_change,
    list_history,
    replace_announcements,
    upload_holding,
)

bp = Blueprint("main", __name__)


@bp.app_errorhandler(RequestEntityTooLarge)
def too_large(_exc):
    flash("上传文件超过 16MB 限制，请拆分或压缩后再传。", "error")
    return redirect(url_for("main.index"))


def _read_file(field: str) -> tuple[str, bytes]:
    fh = request.files.get(field)
    if fh is None or fh.filename == "":
        raise CsvError(f"未选择文件（字段 {field}）")
    return fh.filename, fh.read()


@bp.route("/")
def index():
    result, rows = get_net_change()
    results, batches, holdings = list_history()
    return render_template(
        "index.html",
        result=result,
        rows=rows,
        results=results,
        batches=batches,
        holdings=holdings,
        canon=canon,
    )


@bp.route("/holdings/<period>", methods=["POST"])
def upload(period: str):
    if period not in ("begin", "end"):
        abort(404)
    try:
        as_of = parse_form_date(request.form.get("as_of_date", ""), "日期")
        filename, raw = _read_file("file")
        outcome = upload_holding(period, as_of, filename, raw)
        flash(outcome.message, "success")
        if outcome.result_id is not None:
            return redirect(url_for("result_detail", result_id=outcome.result_id))
    except CsvError as exc:
        flash(f"持仓上传失败：{exc}。原数据未改动。", "error")
    except Exception as exc:  # pragma: no cover - 兜底
        flash(f"持仓上传发生未知错误：{exc}", "error")
    return redirect(url_for("main.index"))


@bp.route("/announcements", methods=["POST"])
def announcements_replace():
    try:
        filename, raw = _read_file("file")
        message, result_id = replace_announcements(filename, raw)
        flash(message, "success")
        if result_id is not None:
            return redirect(url_for("result_detail", result_id=result_id))
    except CsvError as exc:
        flash(f"公告换报失败：{exc}。新表已记为 failed，原生效表继续可用。", "error")
    except Exception as exc:  # pragma: no cover - 兜底
        flash(f"公告换报发生未知错误：{exc}", "error")
    return redirect(url_for("main.index"))


@bp.route("/results/<int:result_id>")
def result_detail(result_id: int):
    result, rows = get_net_change(result_id)
    if result is None:
        abort(404)
    results, batches, holdings = list_history()
    return render_template(
        "index.html",
        result=result,
        rows=rows,
        results=results,
        batches=batches,
        holdings=holdings,
        canon=canon,
    )


@bp.app_template_filter("d")
def decimal_filter(value) -> str:
    return canon(value)
