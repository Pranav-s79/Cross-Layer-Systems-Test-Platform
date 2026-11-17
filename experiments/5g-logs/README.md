# 5G log fixtures (synthetic)

Every log under `logs/` is **SYNTHETIC**. The lines were assembled from the
upstream projects' documented log format and literal logging statements; they
are not a network capture and contain no subscriber data from a real system.

| Profile | Pinned upstream | Primary sources used for the fixture |
|---|---|---|
| `open5gs_amf` | Open5GS `v2.8.0`, commit `157f611a530e292e40ec50f9d23f0ef5d4fcd6a6` | [`src/amf/ngap-handler.c`](https://github.com/open5gs/open5gs/blob/v2.8.0/src/amf/ngap-handler.c), [`src/amf/gmm-sm.c`](https://github.com/open5gs/open5gs/blob/v2.8.0/src/amf/gmm-sm.c), and the [OCUDU/Open5GS COTS UE example](https://docs.ocudu.org/tutorials/cots_ue/) |
| `open5gs_smf` | Open5GS `v2.8.0`, commit `157f611a530e292e40ec50f9d23f0ef5d4fcd6a6` | [`src/smf/npcf-handler.c`](https://github.com/open5gs/open5gs/blob/v2.8.0/src/smf/npcf-handler.c) |
| `ocudu_gnb` | OCUDU `release_26_10`, commit `e0db566aac6e6376ddd60e83c84f58698fcc4d70` | [`lib/ngap/log_helpers.cpp`](https://gitlab.com/ocudu/ocudu/-/blob/release_26_10/lib/ngap/log_helpers.cpp), NGAP/RRC procedure sources under `lib/ngap/procedures/` and `lib/rrc/ue/procedures/`, and the [OCUDU log format](https://docs.ocudu.org/user_manual/outputs/) |

The source locations printed at the end of Open5GS lines are representative
of that pinned tree. They are treated as optional by the profiles because
build layout and source line numbers change between releases.

