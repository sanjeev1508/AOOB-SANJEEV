"""Tests for C → pseudocode compression."""

from __future__ import annotations

from pathlib import Path

from aoob_pipeline.pseudocode import compress_function, enrich_func_payload, parse_snippet_rows
from aoob_pipeline.source_index import SourceIndex


_SIA_FN = r"""
void siaComIfc_10ms(void)
{
    struct T_siaDataIms imsData;
    uint8 tempctr = 0;

        if ((((B_newDatRx) != (0 != 0)) && ((B_newDatTx) != (0 != 0))) || ((B_newTCUDatRx) != (0 != 0)))
        {
                imsData.length = 7;
                imsData.status = 0x02u;
                if((B_newTCUDatRx))
                {
                    for (tempctr=0; tempctr<7u; tempctr++)
                    {
                        imsData.data[tempctr] = sia_TCU_buffer[tempctr];
                    }
                    imsData.command = 0x84u;
                    (B_newTCUDatRx=(0 != 0));
                }
                else
                {
                    for (tempctr=0; tempctr<7u; tempctr++)
                    {
                        imsData.data[tempctr] = sia_buffer[tempctr];
                    }

                    switch (siaCommState)
                    {
                        case 0x41u:
                            imsData.command = 0x81u;
                            break;

                        case 0x42u:
                            imsData.command = 0x82u;
                            break;

                        case 0x43u:
                            imsData.command = 0x83u;
                            break;

                        default:
                            siaComIfc_idle(&imsData);
                            break;
                    }
                    (B_newDatRx=(0 != 0));
                    (B_newDatTx=(0 != 0));
                }

        }
        else
        {
            siaComIfc_idle(&imsData);
        }

    sia_ComIms(&imsData);

    if (0x02u == (uint8)imsData.status)
    {
        if (7u == imsData.length)
        {
            if(imsData.command == 0x44u)
            {
                Com_Tx_VehicleMobilizationState = imsData.data[0];
                Com_Tx_VehicleMobilizationStateStatus = imsData.data[1];
                siaCommState = imsData.command;
            }
            else
            {
                Com_Tx_Sia_TransmitEMS0 = imsData.data[0];
                Com_Tx_Sia_TransmitEMS1 = imsData.data[1];
                Com_Tx_Sia_TransmitEMS2 = imsData.data[2];
                Com_Tx_Sia_TransmitEMS3 = imsData.data[3];
                Com_Tx_Sia_TransmitEMS4 = imsData.data[4];
                Com_Tx_Sia_TransmitEMS5 = imsData.data[5];
                Com_Tx_Sia_TransmitEMS6 = imsData.data[6];
                Sia_472Data___3460 = imsData.data[0];
                Com_Tx_Sia_TxEMS7 =Sia_472Data___3460;

                siaCommState = imsData.command;

                PduInfoType TxPduPtr_t;
                TxPduPtr_t.SduDataPtr = imsData.data;
                TxPduPtr_t.SduLength = 7u;

                if(0x00u == CanIf_Transmit(9, &TxPduPtr_t))
                {
                }

                Sia_flgICUComStart = (1 != 0);

                (B_newDatTx=(1 != 0));
            }
        }
        else
        {
            siaCommErrInd = 0x01u;
        }
    }
    else if (0x00u == imsData.status)
    {
    }
    else
    {
        siaCommErrInd = 0x02u;
    }
}
"""


def _rows_from_c(text: str) -> list[tuple[int, str]]:
    lines = text.strip("\n").splitlines()
    # pretend these start at line 100
    return [(100 + i, line) for i, line in enumerate(lines)]


def test_sia_pseudocode_shrinks_and_keeps_logic() -> None:
    rows = _rows_from_c(_SIA_FN)
    original = "\n".join(f"{ln}: {t}" for ln, t in rows)
    result = compress_function("siaComIfc_10ms", rows)

    # PSEUDO alone should be much smaller than raw C; full snippet includes CITE.
    assert len(result.pseudo) < len(original) * 0.55
    assert "siaComIfc_10ms:" in result.pseudo
    assert "map(siaCommState)" in result.pseudo
    assert "TCU_buf" in result.pseudo or "sia_TCU_buffer" in result.pseudo
    assert "CanIf_Transmit" in result.pseudo
    assert "siaCommErrInd" in result.pseudo
    assert "d.data=" in result.pseudo or "imsData.data" in result.pseudo
    # CITE must keep exact original tokens for quoting
    assert "B_newDatRx" in result.cite or "imsData" in result.cite
    assert "PSEUDO" in result.snippet and "CITE" in result.snippet
    assert "void siaComIfc_10msvoid" not in result.pseudo
    assert "elif if" not in result.pseudo


def test_enrich_payload_keeps_raw(tmp_path: Path) -> None:
    src = tmp_path / "input.c"
    src.write_text(
        "void foo(void) {\n"
        "  if ((flag) != (0 != 0)) { buf[i] = 1; }\n"
        "}\n",
        encoding="utf-8",
    )
    source = SourceIndex(src)
    payload = source.get_func("foo")
    out = enrich_func_payload(payload, focus_tokens={"buf", "i"})
    assert out["raw_snippet"] == payload["snippet"]
    assert out["format"] == "pseudocode+cite"
    assert "PSEUDO" in out["snippet"]
    assert "buf[i]" in out["cite"]


def test_parse_snippet_rows() -> None:
    rows = parse_snippet_rows("10: int x=1;\n11: return x;")
    assert rows == [(10, " int x=1;"), (11, " return x;")]
