"""Bounded XLSX reader for Zhipu bills; only accounting fields leave this module."""
from datetime import date
from decimal import Decimal, InvalidOperation
import io
import re
import xml.etree.ElementTree as ET
import zipfile

from .costs import MODELS

MAX_UPLOAD = 5 * 1024 * 1024
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
FIELDS = {"账单号", "账期(自然日)", "模型编码（推理专用）", "付费类型", "单价", "单价单位", "用量", "用量单位",
          "币种", "总消费金额（结算金额加总）", "请求次数 (仅API)", "价格类型"}
REQUIRED = FIELDS - {"请求次数 (仅API)"}
CATEGORIES = {"输入":"input", "缓存命中":"cached", "输出":"output"}


def integer(value, scale=1, *, negative=False):
    try:
        number = Decimal(str(value)) * scale
        if not number.is_finite() or number != number.to_integral_value() or abs(number) > 2**63-1 or (not negative and number < 0):
            raise ValueError("账单金额或用量不合法")
        return int(number)
    except (InvalidOperation, TypeError):
        raise ValueError("账单金额或用量不合法") from None


def parse_xlsx(data):
    if not data or len(data)>MAX_UPLOAD:
        raise ValueError("请上传不超过 5 MiB 的智谱 XLSX 账单")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        raise ValueError("文件不是有效的 XLSX 账单") from None
    with archive:
        entries = archive.infolist()
        if len(entries)>200 or sum(e.file_size for e in entries)>40*1024*1024 or any(e.file_size>20*1024*1024 for e in entries):
            raise ValueError("XLSX 解压后过大")
        def xml(path):
            raw=archive.read(path)
            if b"\x00" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ValueError("账单包含不支持的 XML 定义")
            return ET.fromstring(raw)
        try:
            shared = ["".join(node.itertext()) for node in xml("xl/sharedStrings.xml").findall("s:si",NS)] if "xl/sharedStrings.xml" in archive.namelist() else []
            sheets=[e.filename for e in entries if re.fullmatch(r"xl/worksheets/sheet\d+\.xml",e.filename)]
            if len(sheets)!=1:
                raise ValueError("首版仅支持单工作表的智谱费用明细")
            rows=xml(sheets[0]).findall(".//s:sheetData/s:row",NS)
            if not rows or len(rows)>50001:
                raise ValueError("账单为空或超过 50000 行")
            def cells(row):
                result={}
                for cell in row.findall("s:c",NS):
                    column=re.sub(r"\d","",cell.get("r", ""))
                    value=cell.find("s:v",NS)
                    inline=cell.find("s:is",NS)
                    text=value.text if value is not None else "".join(inline.itertext()) if inline is not None else ""
                    if cell.get("t")=="s":
                        if not re.fullmatch(r"\d{1,8}", text or ""):
                            raise ValueError("共享字符串索引不合法")
                        text=shared[int(text)]
                    result[column]=text or ""
                return result
            headers=cells(rows[0])
            if not REQUIRED <= set(headers.values()):
                raise ValueError("缺少智谱费用明细所需列")
            allowed={col:name for col,name in headers.items() if name in FIELDS}
            parsed=[]
            for row in rows[1:]:
                values=cells(row)
                item={name:values.get(col,"") for col,name in allowed.items()}
                model=item["模型编码（推理专用）"]
                if model not in MODELS:
                    continue
                category=CATEGORIES.get(item["价格类型"])
                if not category or item["币种"]!="CNY" or item["单价单位"]!="千token" or item["用量单位"]!="token":
                    raise ValueError("不支持的 GLM 计费项目、币种或单位")
                try:
                    day=date.fromisoformat(item["账期(自然日)"]).isoformat()
                except ValueError:
                    raise ValueError("账单日期不合法") from None
                bill_id=item["账单号"]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",bill_id):
                    raise ValueError("账单标识不合法")
                adjustment="减免" in item["付费类型"]
                tokens=integer(item["用量"])
                rate=integer(item["单价"],1_000_000)
                amount=integer(item["总消费金额（结算金额加总）"],1_000_000_000,negative=True)
                if adjustment:
                    if tokens or rate or amount>0:
                        raise ValueError("减免行必须为零用量的非正金额")
                elif amount<0 or amount!=tokens*rate:
                    raise ValueError("账单金额与用量、单价不一致")
                parsed.append({"bill_id":bill_id,"day":day,"model":model,"category":category,
                               "adjustment":int(adjustment),"tokens":tokens,"rate_nano":rate,"amount_nano":amount,
                               "requests":integer(item["请求次数 (仅API)"]) if item.get("请求次数 (仅API)") else None})
            if not parsed:
                raise ValueError("账单没有可导入的 GLM 明细")
            return parsed
        except (ET.ParseError, KeyError, IndexError, zipfile.BadZipFile, UnicodeError, RuntimeError, NotImplementedError):
            raise ValueError("XLSX 内容损坏或格式不受支持") from None
