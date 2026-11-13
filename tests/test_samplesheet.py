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


# Sheets as published in nf-core/test-datasets (illumina/bcl).
NF_CORE_V1 = """[Header]
IEMFileVersion,5

[Reads]
151
151

[Data]
Lane,Sample_ID,Sample_Name,I7_Index_ID,index,I5_Index_ID,index2,Sample_Project
1,Sample1,SampleOne,UDP0001,CAAGACAGAT,UDP0001,ACTATAGCCT,ProjectAlpha
2,Sample4,SampleFour,UDP0004,ATCGCCTGTT,UDP0004,GACTAGCATG,ProjectBeta
"""

NF_CORE_V2 = """[Header]
FileFormatVersion,2

[Reads]
Read1Cycles,151

[BCLConvert_Settings]
OverrideCycles,Y151;I10;I10;Y151

[BCLConvert_Data]
Lane,Sample_ID,Sample_Name,Index,Index2,Sample_Project
1,Sample1,SampleOne,ATTACTCGAT,TATAGCCTGT,ProjectAlpha
2,Sample4,SampleFour,CTCTCTACGC,GGCTCTGAGA,ProjectBeta
"""

NF_CORE_FLOWCELL = """[Header]

[Reads]
110

[Settings]

[Data]
Sample_ID,Sample_Name,Description,Sample_Project
Sample1,Sample1,,
"""


def test_v2_bclconvert_data_section_is_read(tmp_path):
    from seqflow.samplesheet import SampleSheet
    f = tmp_path / "v2.csv"
    f.write_text(NF_CORE_V2)
    sheet = SampleSheet.from_file(f)
    assert [(r.sample_id, r.lane, r.index1, r.index2, r.project) for r in sheet.records] == [
        ("Sample1", 1, "ATTACTCGAT", "TATAGCCTGT", "ProjectAlpha"),
        ("Sample4", 2, "CTCTCTACGC", "GGCTCTGAGA", "ProjectBeta")]


def test_v1_and_a_sheet_without_indexes_are_read(tmp_path):
    from seqflow.samplesheet import SampleSheet
    (tmp_path / "v1.csv").write_text(NF_CORE_V1)
    (tmp_path / "flow.csv").write_text(NF_CORE_FLOWCELL)
    v1 = SampleSheet.from_file(tmp_path / "v1.csv")
    assert v1.records[0].index1 == "CAAGACAGAT" and v1.records[1].lane == 2
    flow = SampleSheet.from_file(tmp_path / "flow.csv")
    assert len(flow) == 1 and flow.records[0].index1 is None and flow.records[0].project is None
