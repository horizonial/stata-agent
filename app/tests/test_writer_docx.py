"""切片 5b：claims → .docx 渲染并可再打开。"""

from __future__ import annotations

from docx import Document

from stata_agent.domain.models import Claim
from stata_agent.writer.docx_out import claims_to_docx, save_docx


def test_claims_to_docx_roundtrip(tmp_path):
    claim = Claim(claim_id="claim-r1", statement="价格随油耗下降（系数=-238.894，样本量=74，R²=0.220）",
                  cards=["card-r1-coef", "card-r1-N", "card-r1-r2"])
    buf = claims_to_docx("主回归结果", [(claim.statement, claim)])
    p = save_docx(buf, tmp_path / "初稿.docx")
    assert p.exists() and p.stat().st_size > 0

    doc = Document(str(p))
    texts = [para.text for para in doc.paragraphs]
    assert any("主回归结果" in t for t in texts)
    assert any("价格随油耗下降" in t and "claim-r1" in t for t in texts)
