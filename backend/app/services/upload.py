from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from ..db import AnalysisWindow, DataQualityIssue, Dataset, FeedbackRecord, MetricSlice
from .golden import new_id


FEEDBACK_COLUMNS = ("feedback_id", "created_at", "source_type", "audience_stage", "country", "text", "link_method")
METRIC_COLUMNS = ("metric_row_id", "window", "period_start", "period_end", "reporting_timezone", "platform", "country", "campaign_id", "creative_id", "currency", "conversion_source", "attribution_window", "spend", "impressions", "reach", "destination_clicks", "installs", "revenue_d7")
FEEDBACK_TEMPLATE = ",".join((*FEEDBACK_COLUMNS, "language", "rating", "platform", "campaign_id", "creative_id")) + "\nfb-001,2026-08-25T12:00:00Z,ad_comment,prospect,BR,I've seen this ad too often,direct_ad_object,en,,Meta,CMP-BR-01,CR-BR-17\n"
METRIC_TEMPLATE = ",".join(METRIC_COLUMNS) + "\nmb-1,baseline,2026-08-18,2026-08-24,UTC,Meta,BR,CMP-BR-01,CR-BR-17,USD,mmp,7d_click,100,10000,5000,250,50,140\nmc-1,current,2026-08-25,2026-08-31,UTC,Meta,BR,CMP-BR-01,CR-BR-17,USD,mmp,7d_click,110,12000,3000,180,22,55\n"
PII = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:\+?\d[\d\s()-]{9,}\d)|\b(?:\d{1,3}\.){3}\d{1,3}\b")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class UploadValidationError(Exception):
    def __init__(self, issues: list[dict]):
        self.issues = issues


def _issue(code: str, file: str, row: int | None, field: str, message: str) -> dict:
    return {"severity": "blocking", "code": code, "file": file, "row": row, "field": field, "message": message}


def _read_csv(raw: bytes, name: str, required: tuple[str, ...], max_bytes: int, max_rows: int) -> list[dict]:
    if not raw or len(raw) > max_bytes or b"\x00" in raw or raw.startswith((b"PK\x03\x04", b"\x89PNG", b"\xff\xd8")):
        raise UploadValidationError([_issue("FILE_INVALID", name, None, "file", "文件为空、超限或不是 CSV 文本。")])
    try:
        decoded = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
        headers = reader.fieldnames or []
        if len(headers) > 40 or len(set(headers)) != len(headers) or any(not header for header in headers):
            raise UploadValidationError([_issue("CSV_HEADER_INVALID", name, 1, "header", "表头为空、重复或超过 40 列。")])
        missing = [column for column in required if column not in headers]
        if missing:
            raise UploadValidationError([_issue("CSV_COLUMN_MISSING", name, 1, column, f"缺少必填列 {column}。") for column in missing])
        rows = []
        for line, row in enumerate(reader, start=2):
            if line > max_rows + 1:
                raise UploadValidationError([_issue("CSV_ROW_LIMIT", name, line, "file", f"最多 {max_rows} 行。")])
            if None in row or any(value is None or len(value) > 2000 or value.lstrip().startswith(("=", "+", "-", "@")) for value in row.values()):
                raise UploadValidationError([_issue("CSV_CELL_INVALID", name, line, "row", "列数错误、单元格过长或包含公式型内容。")])
            rows.append({key: value.strip() for key, value in row.items()})
        if not rows:
            raise UploadValidationError([_issue("CSV_EMPTY", name, None, "file", "至少需要一行数据。")])
        return rows
    except UnicodeDecodeError:
        raise UploadValidationError([_issue("CSV_ENCODING", name, None, "file", "仅支持 UTF-8 或 UTF-8 BOM。")]) from None
    except csv.Error:
        raise UploadValidationError([_issue("CSV_PARSE", name, None, "file", "CSV 结构无法解析。")]) from None


def _nonnegative(row: dict, field: str, line: int, integer: bool) -> Decimal:
    try:
        value = Decimal(row[field])
        if not value.is_finite() or value < 0 or (integer and value != value.to_integral_value()):
            raise InvalidOperation
        return value
    except (InvalidOperation, KeyError):
        raise UploadValidationError([_issue("METRIC_NUMBER", "ad_metrics.csv", line, field, f"{field} 必须是非负{'整数' if integer else '数字'}。")]) from None


def validate(feedback_raw: bytes, metrics_raw: bytes) -> tuple[list[dict], list[dict], dict, list[dict]]:
    feedback = _read_csv(feedback_raw, "feedback.csv", FEEDBACK_COLUMNS, 1_000_000, 500)
    metrics = _read_csv(metrics_raw, "ad_metrics.csv", METRIC_COLUMNS, 2_000_000, 5000)
    issues: list[dict] = []
    warnings: list[dict] = []
    windows: dict[str, tuple[date, date]] = {}
    keys: set[tuple] = set()
    ids: set[str] = set()
    currencies: set[str] = set()
    normalized_metrics: list[dict] = []
    for line, row in enumerate(metrics, 2):
        try:
            start, end = date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"])
            if (end - start).days != 6:
                raise ValueError
        except ValueError:
            issues.append(_issue("WINDOW_INVALID", "ad_metrics.csv", line, "period_start", "窗口必须是完整的连续 7 日。"))
            continue
        window = row["window"]
        if window not in {"baseline", "current"} or row["reporting_timezone"] != "UTC" or row["attribution_window"] != "7d_click" or row["conversion_source"] not in {"mmp", "platform", "synthetic"}:
            issues.append(_issue("METRIC_ENUM", "ad_metrics.csv", line, "window", "仅允许 baseline/current、UTC、7d_click。"))
            continue
        if not re.fullmatch(r"[A-Z]{2}", row["country"]) or not re.fullmatch(r"[A-Z]{3}", row["currency"]) or any(not SAFE_ID.fullmatch(row[field]) for field in ("platform", "campaign_id", "creative_id", "metric_row_id")):
            issues.append(_issue("METRIC_IDENTIFIER", "ad_metrics.csv", line, "creative_id", "国家、币种或指标关联键格式不合法。"))
        if window in windows and windows[window] != (start, end):
            issues.append(_issue("WINDOW_MISMATCH", "ad_metrics.csv", line, "period_start", "同一窗口的日期必须一致。"))
        windows[window] = (start, end)
        key = (window, row["platform"], row["country"], row["campaign_id"], row["creative_id"])
        if not all(key) or key in keys or not row["metric_row_id"] or row["metric_row_id"] in ids:
            issues.append(_issue("METRIC_KEY", "ad_metrics.csv", line, "creative_id", "指标关联键为空或重复。"))
        keys.add(key)
        ids.add(row["metric_row_id"])
        currencies.add(row["currency"])
        try:
            numbers = {field: _nonnegative(row, field, line, field in {"impressions", "destination_clicks", "installs"}) for field in ("spend", "impressions", "destination_clicks", "installs", "revenue_d7")}
            reach = None if not row["reach"] else _nonnegative(row, "reach", line, True)
            if numbers["installs"] > numbers["destination_clicks"] or numbers["destination_clicks"] > numbers["impressions"] or (reach is not None and (reach > numbers["impressions"] or reach == 0)):
                issues.append(_issue("METRIC_BOUNDS", "ad_metrics.csv", line, "installs", "需满足 installs ≤ clicks ≤ impressions，0 < reach ≤ impressions。"))
            if reach is None:
                warnings.append({**_issue("REACH_MISSING", "ad_metrics.csv", line, "reach", "Frequency 不可计算，素材疲劳结论降级。"), "severity": "warning"})
            normalized_metrics.append({**row, "window": window, "start": start, "end": end, "numbers": numbers, "reach_value": reach})
        except UploadValidationError as exc:
            issues.extend(exc.issues)
    if len(currencies) != 1 or not all(currencies):
        issues.append(_issue("CURRENCY_MIXED", "ad_metrics.csv", None, "currency", "一次分析只允许一种非空币种。"))
    if set(windows) != {"baseline", "current"} or (set(windows) == {"baseline", "current"} and windows["baseline"][1] + timedelta(days=1) != windows["current"][0]):
        issues.append(_issue("WINDOW_PAIR", "ad_metrics.csv", None, "window", "必须有相邻且不重叠的 baseline/current 7 日窗口。"))
    scopes = {(key[1], key[2], key[3], key[4]) for key in keys}
    for scope in scopes:
        if not all((window, *scope) in keys for window in ("baseline", "current")):
            issues.append(_issue("METRIC_PAIR", "ad_metrics.csv", None, "creative_id", f"{scope[3]} 缺少成对窗口。"))
    normalized_feedback: list[dict] = []
    ids.clear()
    for line, row in enumerate(feedback, 2):
        if not row["feedback_id"] or row["feedback_id"] in ids:
            issues.append(_issue("FEEDBACK_ID", "feedback.csv", line, "feedback_id", "反馈 ID 为空或重复。"))
        ids.add(row["feedback_id"])
        if row["source_type"] not in {"ad_comment", "new_user_survey", "app_store_review"} or row["audience_stage"] not in {"prospect", "new_user", "existing_user", "unknown"}:
            issues.append(_issue("FEEDBACK_ENUM", "feedback.csv", line, "source_type", "来源或受众阶段不在允许范围。"))
        if not SAFE_ID.fullmatch(row["feedback_id"]) or not re.fullmatch(r"[A-Z]{2}", row["country"]) or any(row.get(field) and not SAFE_ID.fullmatch(row[field]) for field in ("platform", "campaign_id", "creative_id")):
            issues.append(_issue("FEEDBACK_IDENTIFIER", "feedback.csv", line, "feedback_id", "反馈 ID、国家或广告关联键格式不合法。"))
        if not row["country"] or not 1 <= len(row["text"]) <= 2000 or PII.search(row["text"]):
            issues.append(_issue("FEEDBACK_PRIVACY", "feedback.csv", line, "text", "国家或文本为空，或文本疑似包含个人标识。"))
        if row["source_type"] == "ad_comment" and (row["audience_stage"] != "prospect" or row["link_method"] != "direct_ad_object" or not all(row.get(field) for field in ("platform", "campaign_id", "creative_id"))):
            issues.append(_issue("FEEDBACK_LINK", "feedback.csv", line, "creative_id", "广告评论须为 prospect、direct_ad_object，并关联平台、Campaign 与 Creative。"))
        if row["source_type"] == "new_user_survey" and (row["link_method"] != "mmp_attribution" or not all(row.get(field) for field in ("platform", "campaign_id"))):
            issues.append(_issue("FEEDBACK_LINK", "feedback.csv", line, "link_method", "归因调研须为 mmp_attribution，并至少关联平台与 Campaign。"))
        if row["source_type"] == "app_store_review" and (row["link_method"] != "none" or row.get("creative_id") or row.get("campaign_id")):
            issues.append(_issue("FEEDBACK_LINK", "feedback.csv", line, "creative_id", "商店评论仅允许 link_method=none，不得声称 Creative/Campaign 级关联。"))
        if row.get("rating") and (not row["rating"].isdigit() or not 1 <= int(row["rating"]) <= 5):
            issues.append(_issue("FEEDBACK_RATING", "feedback.csv", line, "rating", "评分须为 1–5。"))
        try:
            created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None or created.utcoffset() != timedelta(0):
                raise ValueError
            created_date = created.date()
        except ValueError:
            issues.append(_issue("FEEDBACK_DATE", "feedback.csv", line, "created_at", "时间必须是 UTC RFC3339。"))
            continue
        window = next((name for name, (start, end) in windows.items() if start <= created_date <= end), None)
        if window is None:
            warnings.append({**_issue("FEEDBACK_OUTSIDE_WINDOW", "feedback.csv", line, "created_at", "该反馈不在分析窗口内，不参与关联。"), "severity": "warning"})
        normalized_feedback.append({**row, "window": window or "outside"})
    if issues:
        raise UploadValidationError(issues[:30])
    return normalized_feedback, normalized_metrics, windows, warnings[:30]


def store_dataset(session: Session, workspace_id: str, feedback_raw: bytes, metrics_raw: bytes) -> tuple[Dataset, list[dict]]:
    feedback, metrics, windows, warnings = validate(feedback_raw, metrics_raw)
    digest = hashlib.sha256(feedback_raw + b"\x00" + metrics_raw).hexdigest()
    dataset = Dataset(id=new_id("ds"), workspace_id=workspace_id, source="upload", status="ready", name="自定义匿名 CSV", feedback_rows=len(feedback), metric_rows=len(metrics), sha256=digest)
    session.add(dataset)
    session.flush()
    for name, (start, end) in windows.items():
        session.add(AnalysisWindow(id=new_id("win"), dataset_id=dataset.id, window_type=name, start_date=start.isoformat(), end_date=end.isoformat()))
    for row in feedback:
        session.add(FeedbackRecord(id=new_id("fb"), workspace_id=workspace_id, dataset_id=dataset.id, window_type=row["window"], locale=row.get("language") or "und", market=row["country"], platform=row.get("platform", ""), campaign_id=row.get("campaign_id", ""), creative_id=row.get("creative_id", ""), source=row["source_type"], created_at=row["created_at"], audience_stage=row["audience_stage"], link_method=row["link_method"], topic="unclassified", text=row["text"]))
    for row in metrics:
        numbers = row["numbers"]
        session.add(MetricSlice(id=new_id("metric"), workspace_id=workspace_id, dataset_id=dataset.id, window_type=row["window"], market=row["country"], platform=row["platform"], campaign_id=row["campaign_id"], creative_id=row["creative_id"], spend=float(numbers["spend"]), impressions=int(numbers["impressions"]), reach=int(row["reach_value"]) if row["reach_value"] is not None else None, clicks=int(numbers["destination_clicks"]), installs=int(numbers["installs"]), revenue_d7=float(numbers["revenue_d7"]), currency=row["currency"], conversion_source=row["conversion_source"], attribution_window=row["attribution_window"]))
    for warning in warnings:
        session.add(DataQualityIssue(id=new_id("issue"), dataset_id=dataset.id, severity="warning", code=warning["code"], message=f'{warning["file"]}:{warning["row"]} {warning["message"]}'))
    return dataset, warnings
