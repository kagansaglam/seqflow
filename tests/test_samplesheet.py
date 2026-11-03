import pytest

from seqflow.exceptions import SampleSheetError
from seqflow.samplesheet import SampleSheet


def test_parses_valid_sheet(illumina_run):
    sheet = SampleSheet.from_file(illumina_run / "SampleSheet.csv")
    assert len(sheet) == 3
    assert sheet.records[0].index1 == "ACGTACGT"
    assert sheet.records[2].project == "projB"


def test_missing_file(tmp_path):
    with pytest.raises(SampleSheetError, match="not found"):
        SampleSheet.from_file(tmp_path / "nope.csv")


def test_no_data_section(tmp_path):
    f = tmp_path / "s.csv"
    f.write_text("[Header]\nfoo,bar\n")
    with pytest.raises(SampleSheetError, match="no \\[Data\\]"):
        SampleSheet.from_file(f)


def test_duplicate_sample_id(tmp_path):
    f = tmp_path / "s.csv"
    f.write_text("[Data]\nSample_ID,index,index2\nA,ACGT,TTTT\nA,GGGG,CCCC\n")
    with pytest.raises(SampleSheetError, match="duplicate"):
        SampleSheet.from_file(f)


def test_index_collision(tmp_path):
    f = tmp_path / "s.csv"
    f.write_text("[Data]\nSample_ID,index,index2\nA,ACGT,TTTT\nB,ACGT,TTTT\n")
    with pytest.raises(SampleSheetError, match="collision"):
        SampleSheet.from_file(f)


def test_invalid_index_characters(tmp_path):
    f = tmp_path / "s.csv"
    f.write_text("[Data]\nSample_ID,index,index2\nA,ACGX,TTTT\n")
    with pytest.raises(SampleSheetError, match="invalid index"):
        SampleSheet.from_file(f)


def test_mixed_index_lengths(tmp_path):
    f = tmp_path / "s.csv"
    f.write_text("[Data]\nSample_ID,index,index2\nA,ACGT,TTTT\nB,GGGGAA,CCCC\n")
    with pytest.raises(SampleSheetError, match="mixed index1"):
        SampleSheet.from_file(f)
