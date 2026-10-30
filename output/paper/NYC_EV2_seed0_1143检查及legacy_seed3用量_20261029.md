# NYC EV2 seed0 11:43 检查及 legacy seed3 F2 用量

证据类型：EV2 v4官方结果审计、独立冻结输入/方法复核、只读v5侧车、legacy seed3单元级结果与usage审计。不同版本不合并。

## EV2 v4 seed0

- 官方快照 `ev2_fullmatrix_audit_20261029T1143CST.json` SHA-256：`0e6dfd5006300a5ef37d0c305b810955e0ed97c30ce032dd9e74bdeab67d5928`。269/630方法单元完整有效、361缺失、0无效，共136,070轮。D0/D1/D2/D5/D6/D7/D8覆盖86/86/86/2/3/3/3；86个已出现的场景—车队组合中仅2个齐备7法，不能报告总体方法排名或效果。
- 冻结输入/方法核验 `ev2_input_fingerprint_audit_20261029T1143CST.json` SHA-256：`54de39be38e213718d87cd6681c966f3e87822a2d3dc2701e8990de35604baf6`。269个结果均匹配scenario/orders/OD矩阵等冻结输入、同格场景指纹、方法契约、逐轮validator及EV profile；0 mismatch、0错误。EV profile SHA-256为`b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。
- 最新确认的完整新格 `EV_20241115_224000_q75/F1` 的D0/D1/D2已完成并通过该快照。D0/D1/D2结果SHA分别为`461de4cafc6a28baee43046b8c29cfa8cf036d44df2c058012b96ebc62c1b7d9`、`4e38b1b95cded69c1cfb272d5ecd212be71a8165a5e845b6ef2580040541c4ed`、`a3eac3b6b2940c6943f3cb9985e8e42a69733c639c96f93ef5346c0cc3454479`。
- v5补充侧车manifest `time_money_v5_segment_sidecar_20261029T1143CST_supplement_to_269/manifest.json` SHA-256：`ffd2d8d03e7e00f0e3f7bcf4f8154cd951aef2ea505beb3e21f5d08c8baea0b9`。本次核算129个来源run、7,616段，与前序140项合计覆盖269个结果。129项全blocked/null，0排名；缺冻结电价与完整共同候选池。模型请求0、SUMO重跑0、来源结果未改。

## 旧版多月 seed3：F2 的 D5/D6/D7/D8

旧版seed3使用`dispatch-agent-v1`及`nyc_dispatch_decision_contract_v2`，只作隔离结果记录。

- D5结果SHA-256 `ea0478ef62cc7aca41b8127f25769903cb1c1ab3804881ec7b34e8282d1a84d1`：完整仿真，534轮validator通过、0硬约束/一对一错误。10次Codex调用中9条策略记录accepted、1条`llm_error_or_invalid_response`；524轮无可行候选而未请求。trace为10个completed、0 failed、0 CLI error/429。Usage合计178,839 tokens（input 176,158，其中cached 38,016；output 2,681）；rate-card估算0.790148 Codex标准额度等值/$0.03160592 API等值，账户实际账单未知。仿真完成不表示所有LLM返回有效。
- 同格D8完整但唯一策略响应为`invalid_policy_parameters`（0 accepted）；D6作为独立计划方法单元完成，响应accepted；D7按D6接受响应离线回放且没有新增模型请求。D8/D6/D7均为536轮，validator通过。具体结果SHA、prompt hash与成本估算见 `NYC_seed0_EV2审计与legacy_seed3消耗_20261029T1139CST.md`。
- F2现有D0/D1/D2/D5/D6/D7/D8七法的场景指纹相同，scenario/orders/zone-OD矩阵输入SHA全部匹配冻结`benchmark_manifest.json`。

## 在途

11:43 CST时，seed0 runner已进入`EV_20241115_224000_q75/F2/D0_D1`；legacy seed3 runner仍在`EV_20241005_225000_q75/F4/D0_D1`。两者均为确定性计算。已知失败/未知投递仍保留原状态，不重试、不重放。

NYC订单只映射到zone-level OD成本代理，接驾采用合成质心代理；不是街道级路线或实时ETA。EV2是模拟电耗，不是车辆遥测。


## 2026-10-29 11:55 CST 增量：EV2 seed0 三方法格与分段账本

证据类型：EV2 v4官方结果审计、独立输入/方法指纹复核、v5唯一增量侧车；旧版seed3执行状态单独记录。

- seed0官方快照 `ev2_fullmatrix_audit_20261029T1155CST.json` SHA-256 `0e7997a51bc401453ee2f814b82fa758cbbff7d762377c4326594e669e60b527`：272/630结果完整有效、358缺失、0无效，共137,687轮。D0/D1/D2/D5/D6/D7/D8计数为87/87/87/2/3/3/3；87个已有场景—车队格只有2格齐备七法，不能给出总体方法排名或效果结论。
- 输入指纹复核 `ev2_input_fingerprint_audit_20261029T1155CST.json` SHA-256 `1ff99954afe6d163b09726c19c90a6f3f4fd9c1ba442e20422e625bc03f62f2f`：272个结果均匹配冻结输入、同格指纹、EV2方法契约及profile；场景/输入mismatch和逐run错误均为0。Profile SHA-256 `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。
- `EV_20241115_224000_q75/F2` 的D0、D1、D2同格单元均为539轮、48个行程段、0无效轮，结果SHA分别为 `ba1b899198032fd586f6cfd02ddd6fc553c93dc7518e2827e2a64d4831bdb1ba`、`4ea349b37ed701eaefd223710c9e1b0f5564a213f747ef41597e346b0b634c5c`、`958be781aa42f516e1ee44020cfc58ca4b07961a95c4b18ec8ec80f3797c3b36`。这只补齐该格前三种方法，不是完整七法格。
- v5唯一增量侧车：截至本快照唯一覆盖272个来源结果。T1152 delta为新增D0/D1两项、96段，manifest SHA `9f881b70aa44ab3827a2564a4e1d4daa52a9b2f71af4be7cbb09ab58533d8964`；T1155 delta为新增D2一项、48段，manifest SHA `f7f199dfd894cd56bed8011a4e1d0b5eae636412db2ac05f71b776a0471d75b9`。两批逐段核对 `billable_energy_kwh=max(signed_net_kwh,0)`，0 mismatch；乘客收入仅保留历史票价代理标记、空驶收入为0。两批均缺场景电价、完整票价交叉表、方法计算费率及同一时点完整共同候选池，路线/利润评分blocked、0排名；0模型请求、0 SUMO重跑、源结果哈希复核通过且未改写。
- 侧车血缘说明：T1150中间侧车按直接前序账本处理了142项，其中140项已由更早侧车覆盖。该文件保留但不作为唯一增量计数；T1152/T1155以去重覆盖索引生成最小delta，实际新增来源run为3项。侧车只是离线核算，不代表time-money selector在线切换。
- 11:55 CST在途：EV2 seed0已转到 `EV_20241115_224000_q75/F4/D0_D1` 确定性计算；legacy seed3已从F4/D2完成转入D8 live运行。D8 trace已有一个已完成响应，usage为13,453 input + 232 output tokens；整项仿真/validator尚未完成，因此不计入完成结果，失败/未知不重试。

NYC仍是TLC真实订单映射到zone-level OD成本代理，接驾为质心代理；不是街道级路网或实车ETA。EV2为模拟能耗，不是遥测。

- 为后续增量保留去重覆盖索引 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_ev2_segment_ledger_v4_20261027/seed_0/time_money_v5_source_coverage_index_20261029T1155CST_272/manifest.json` SHA-256 `6b75f9564a279ef00446ccd593d15d31ac7c5fb14a20d3552c2656b808b2d34b`；含272个唯一来源哈希，只作离线增量去重索引，不是决策侧车。


## 2026-10-29 12:01 CST 增量：legacy seed3 F4/D8 已完成单元复核

证据类型：旧版seed3单元结果、冻结package输入SHA、Codex CLI原始trace及逐轮validator复核。该结果不并入EV2 v4 seed0。

- `EV_20241005_225000_q75/F4/D8_agent_codex_nn_capacity_shield_optimizer_seed_3.json` SHA-256 `2a8a07d7c0eb7c77f3c540a31107107da6fd9ba1816165a347a7d005cab74a00`：`dispatch-agent-v1`，`partial_run=false`，完整仿真550轮；550/550轮`validator_status=valid`、一对一检查通过、硬约束错误0、validation errors 0。该格552个需求订单中50 completed、502 unserved；D0/D1/D2同格也各完成50单，但此旧版单格不构成总体性能差异结论。
- 该格与D0/D1/D2使用同一scenario fingerprint及同一输入SHA；scenario/order/zone-OD矩阵SHA分别匹配冻结`benchmark_manifest.json`（manifest SHA-256 `262d067a59a35ffe9b299a49f1cc86868bba19713cfed2361d0d287dd2f95d22`）。D0/D1/D2结果SHA分别为 `61881a29071346a061e9bbd1c27a0b60781e28b4cdd0f1762220a4337a57f774`、`3761bbc56646ca04726914e33077e735388edc21c8758052620c5f1f762b7e5b`、`fb2c594e0dee122a00c33b0ddd720ebb3274ccc6cbe33371fd983cc0ea6b10d9`。
- D8的单次Codex请求状态accepted，CLI exit 0，retry_count 0；usage为13,453 input +232 output =13,685 tokens，标准额度rate-card估值0.074225、API等值$0.002969，实际账单未由CLI报告。原始trace SHA-256 `d46a3535bf367e7b9fa66cfa6f55bd148784ceab44cda7ec14da2cd0dd7b9006`。政策输出可被接受不等于整体方法优势。
- 当前legacy runner正使用同一已保存D8政策零新增请求地回放D6/D7；该复用单元与独立LLM调用分开记账。EV2 v4 seed0则仍在独立F4的D0/D1确定性计算中。


## 2026-10-29 12:09 CST 增量：EV2 seed0 F4 两个基线完成并审计

证据类型：EV2 v4官方结果账本、独立输入/方法指纹复核、两项v5唯一增量侧车。

- 快照 `ev2_fullmatrix_audit_20261029T1209CST.json` SHA-256 `0dee0daf4b972cb0030ae3c90bedc718a51349b085815dbee2b47ff398364d37`：274/630有效、356缺失、0无效、138,795轮。D0/D1/D2/D5/D6/D7/D8计数为88/88/87/2/3/3/3；88个已有场景—车队格中仅2格齐备七法，不能支持全矩阵排序或总体效果结论。
- 独立指纹快照 `ev2_input_fingerprint_audit_20261029T1209CST.json` SHA-256 `58cd799024ed67b15ab8b45380e869bf4146fd38ff1a9a69a7404b3fba375799`：274项均匹配冻结输入、同格fingerprint、方法契约和profile；0 scenario/input mismatch、0 run/cell错误；EV2 profile SHA `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。
- `EV_20241115_224000_q75/F4` 的D0/D1均554轮、98段、0无效轮，SHA分别为 `0124a7276852d28b104f5e20eec067419153718a25635a9fe69bdbc32460ecf7` 与 `bce72bb0d228f360bc9caf357957c7262df5ceeb3ea81a4658199a07e6cdd117`。此格D2仍在运行，不能把该格计作七法完整格。
- v5 T1209唯一增量侧车 `time_money_v5_segment_sidecar_20261029T1209CST_unique_delta_2/manifest.json` SHA-256 `a605a929f32c4991fd72d6870d927f9bba885fbe35fe81031eaeb3dd8d8ba2c3`：只处理上述2个新增run/196段（空驶98、载客98），signed-net截零计费公式核对0 mismatch。历史票价代理未被当成实际营收，电价、完整fare crosswalk、方法计算费率及共同候选池均缺失；全部blocked/null、0排名，0模型请求、0 SUMO重跑，源哈希复核通过、源结果未变。
- 去重source coverage index现扩展到274项，manifest `time_money_v5_source_coverage_index_20261029T1209CST_274/manifest.json` SHA-256 `ab8ca659eea4e993bc4b84ad9c38cd3fd375c2be555d78fb405e19e72c8816f2`；只用于后续离线delta去重。
- 12:11 CST在途：seed0 EV2 F4/D2确定性单元和legacy seed3 F4/D6-D7无新调用回放均有活跃CPU进程。后者复用已审计D8响应，不能按两次新LLM调用计费。


## 2026-10-29 12:14 CST 增量：EV2 seed0矩阵与9项新增结果审计

证据类型：EV2 v4正式矩阵审计、独立输入指纹审计，以及与GitHub 269项检查点的逐项结果哈希比较。

- 文件名为`ev2_fullmatrix_audit_20261029T1213CST.json`的矩阵快照SHA-256 `dca0e81bbc9394a43236f3e390fa25ba51ed0763c3fd0e87261d1ec91a5a6b6c`，审计时间12:14:24Z：278/630有效结果、352缺失、0无效。独立输入指纹快照`ev2_input_fingerprint_audit_20261029T1213CST.json` SHA `d45ba84a4196cb297d0b4b69000f0154289e0d372de821304a1a440a3def5edd`，审计时间12:14:52Z：278项均匹配冻结输入及同格fingerprint，0 mismatch。方法计数为D0/D1/D2各89，D5为2，D6/D7/D8各3。
- 与GitHub上截至11:43审计的269项基线比较，新增9个D0/D1/D2结果；原有269项结果SHA全部不变。新增单元为`EV_20241115_224000_q75/F2`（各539轮）、同场景F4（各554轮）及`EV_20241222_205500_q25/F1`（各412轮）。9个run均`partial_run=false`，结果SHA与源文件逐项一致，0无效轮、0硬约束错误。单项校验和记录在本次增量档案manifest及`SHA256SUMS`。
- 该聚合仍是278/630部分矩阵；不用于全局排名或总体方法效果结论。最新审计及只包含这9项的无损增量包归档于`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/repository_archives_20261029T1226CST/`。


## 2026-10-29 12:36 CST 增量：EV2七方法完整格、离线v5侧车及legacy seed3进度

证据类型：EV2 v4全矩阵审计与冻结指纹复核、逐轮validator/原始CLI trace核对、唯一调用账本、v5离线增量侧车；另附隔离legacy seed3基线复核。EV2 seed0与legacy seed3不合并。

### EV2 v4 seed0

- 12:29 CST官方快照`ev2_fullmatrix_audit_20261029T1229CST.json` SHA-256 `fde185b1b834a267f163573621f89f777dd92508335f0021c22179d0b6d72ce7`：286/630方法结果有效完整、344缺失、0无效，共144,285轮。D0/D1/D2各90项，D5为3项，D6/D7各4项，D8为5项；90个场景—车队格中3格完成七方法。该矩阵仍不完整，不能报告全矩阵排序或总体效果。
- 独立输入指纹复核`ev2_input_fingerprint_audit_20261029T1229CST.json` SHA-256 `29044c304d4d5a1ff848a4193d5494fdf15fe4e32126b2312c3ed3c9be6904cd`：286项冻结输入、同格场景指纹、方法契约均匹配，mismatch和逐项错误均0；EV2 profile SHA-256仍为`b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。
- 新完整格`EV_20240321_220000_q50/F1`：D0/D1/D2/D5/D6/D7/D8分别482/483/482/483/482/483/483轮，均24个行程段、逐轮validator均valid、one-to-one均通过、硬约束错误0；结果哈希依序为`4a801966e3de48bf20a18b2ac623ee91c82ea7224048b2e4400979fbd5d64b72`、`6a23e6db24d0f767dba65361e3c25ae09c579230b96f60b56518681155fcd0bd`、`a8e039ae65bbd1f8324052aa0c1939f836696b772f61b4ae4a5e84fb6bae3531`、`823c0435e020076c75c1d58e2261cb47514cfd143191b7a2fbaace525cde80ac`、`ef69117ee989a3dae5a9a68aab801c320bd835aea004eed0bad02c39138dd1f7`、`8f92922faefc627a8ee2fd45c55db49df31a2f5673aefad31bf4532fc8faa140`、`325991bfc79677fd09b54276c73a309ffae806010e01d6260be072a81da2ab7b`。七项均属同一冻结场景/输入；仅代表该单格。
- 该格每种方法服务12/502单。D0/D2/D6的平均接驾等待107.115秒、空驶6.427公里、模拟EV2总电耗9.810618 kWh；D1/D5/D7/D8分别为135.262秒、8.116公里、9.822496 kWh。只记录描述性配对结果，不外推为总体差异或方法优势。方法目标核验：D0分配规则不使用能耗；D5把EV2接驾/载客signed-net能耗提供给LLM，但不接确定性全局能耗优化器；D1/D2/D6/D7/D8均将EV2 signed-net总能耗作为冻结词典序全局优化目标的末级。
- 唯一调用账本`audit_snapshots/unique_invocation_audit_EV_20240321_F1_20261029T1232CST.json` SHA-256 `683a6909b045d87c662635f2e8c9af6d457c12235a3bba41777004a5908c7406`：D5有6次独立完成调用，D8有1次共享策略调用；D6/D7按冻结政策复用D8同一响应，无新增调用。独立调用合计7次，112,598 input tokens（其中cached 63,360）、1,501 output、总计114,099 tokens；标准额度估值0.3229，API等值估算$0.012916，Codex CLI未报告账户实扣。D5本格6个raw trace均`turn.completed`且0重试。全矩阵截至该快照D5 trace共27条，其中15条有usage的完成响应、12条429失败且usage不可用；保留失败与未知成本，不重试，不将其填为0。旧`EV_20240312_200000_q25/F4/D5`仍是`failed_no_retry`且结果文件缺失；runner记录后续`blocked`并禁止自动重试。
- v5唯一增量`time_money_v5_segment_sidecar_20261029T1229CST_unique_delta_4/manifest.json` SHA-256 `4af13b4724bdafe72a5418ff40ac2d1da080bb340dd00a167dbbc371b342aed4`，新增4个来源run、120段（60空驶、60载客）。独立分段账本复核`audit_snapshots/time_money_v5_delta_audit_20261029T1234CST.json` SHA `2925c02bf07998061fb4309fe4ee973655f1ff036da2dff972c9ce569412e2a3`：按source hash、candidate/segment id及segment type逐项对账，`max(signed_net_kwh,0)` mismatch 0、来源哈希变化0、空驶收入异常0、重复乘客服务收入0。电价/完整票价交叉表/方法计算费率/共同路线候选池不足；4/4路线经济评分blocked/null、0排名、0新模型调用、0 SUMO重跑，未改动来源结果。该v5侧车为离线审计，不是在线selector。
- 后续增量去重索引`time_money_v5_source_coverage_index_20261029T1229CST_286/manifest.json` SHA-256 `b732142be08a5ee7e9916df7933c0151a003598122caeba3ee4fdb22a8b064fe`，含286个唯一source result哈希，仅供下一次离线delta去重。
- 截至12:35 CST，官方runner仍运行：`EV_20240321_220000_q50/F2`的D8已完成（494轮、48段，SHA `036ea641f96190943fc077bb0fc4c8c1c9c10b576bd491098884795ff88b9e57`），D6/D7随后无新增请求回放完成（SHA分别`5235b818b2e6c62684c3bd3be27f8767e9c390fa335865f0965888dd93da5c41`和`d6aaee3a0d68eda453b8d71e241227cc827e4b6e8b8675aa9240d96b89b17592`）；D5于12:34 CST启动live调用，12:35仍在运行，尚未纳入上述快照或侧车。待其终态后再做全矩阵及唯一增量侧车复核。

### 隔离legacy multi-month seed3

- `EV_20241111_200000_q25/F2`的D0/D1/D2基线均已结束：每项490轮，490/490 validator valid、one-to-one通过、硬约束错误0；同一scenario fingerprint和scenario/orders/zone-OD输入SHA。三项各服务24/393单。结果SHA分别为`3c4ebc818051a612dfbe285c2f346b48fb9890492e19b0d217ce444322416f08`、`e20de2c63b104314b58f0e70316c4ae1bf635935c7a8c6b17e847e0d8e6757df`、`eef3b2caaea9619178fb50e78df324d12a75cfe304a1291364ae339e0374294c`。机器可读审计`isolated_legacy_cell_audit_EV_20241111_F2_seed3_20261029T1236CST.json` SHA `7a218b7b13fc4a80196073454ecfe9aed5c298c36dd3c048126c1e429dbb78ba`。这只是旧schema下的三个基线，不并入EV2 v4，也不是LLM比较结果。
- 截至12:35 CST，legacy seed3 runner已启动同格D8 live；之前D0/D1、D2均已完成。继续单独复核该旧批次的D5/D6/D7/D8、唯一调用usage和validator，不与seed0 EV2结果或EV2 sidecar混算。


## 2026-10-29 12:42 CST 增量：EV2第二个七法格审计及legacy seed3 F2进展

证据类型：EV2 v4官方矩阵/指纹审计、逐轮validator、原始Codex CLI usage、唯一调用审计及v5离线sidecar。

- T1240 EV2 seed0快照`ev2_fullmatrix_audit_20261029T1240CST.json` SHA-256 `9f95869d3a4fc1e1ab3275430fdcad2a95ae2e56ab228267d3eca28c8fca8c7d`：289/630有效、341缺失、0无效、145,766轮。方法覆盖D0/D1/D2各90、D5为4、D6/D7各5、D8为5；90个场景—车队格中4格七方法齐全。冻结输入指纹审计SHA `923e78b69899ad4c470083613600488bc481860c671c9ab91b0dc54abde79f97`：289项输入、同格场景和契约mismatch均0；EV2 profile SHA仍为`b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。
- 新完整格`EV_20240321_220000_q50/F2`七法每项48段、逐轮validator通过、invalid round为0、one-to-one和硬约束错误为0；结果SHA按D0/D1/D2/D5/D6/D7/D8顺序为`ee6101a122cadbeeda1b86744539829cee9ea513a808916b560bb1bca0d4936a`、`a60ce689ae8291fe5c1868c815de0dad25f00d4f2fbcb865d855ea7d54a4ffac`、`9f4d95d8d5d131f6e49dc61bfa9b26906cd6a6e732fcdf64ed36f16057ff7c77`、`984b4c4029adc5314501677bb796e2300aa265617e5261a7b9bb14f6554fa755`、`5235b818b2e6c62684c3bd3be27f8767e9c390fa335865f0965888dd93da5c41`、`d6aaee3a0d68eda453b8d71e241227cc827e4b6e8b8675aa9240d96b89b17592`、`036ea641f96190943fc077bb0fc4c8c1c9c10b576bd491098884795ff88b9e57`。各法均服务24/502单。平均等待/空驶距离/模拟EV2总能耗按D0至D8次序为：D0 114.722秒/13.767km/19.591934kWh；D1 114.722秒/13.767km/19.239983kWh；D2同D1；D5 118.113秒/14.164km/19.279063kWh；D6 107.501秒/12.900km/19.154247kWh；D7同D6；D8 107.501秒/12.900km/19.506197kWh。仅属该同格描述结果，不外推总体优势。按冻结目标，D0不优化能耗、D5向LLM给出EV2但不做确定性全局能耗优化；D1/D2/D6/D7/D8以EV2 signed-net总能耗作为词典序末级。
- 新格唯一调用账本`audit_snapshots/unique_invocation_audit_EV_20240321_F2_20261029T1242CST.json` SHA-256 `0dc689bcbcaee36e15fe73f14e8949011784f05559cf2e8ff404a39b75e32dd5`：D5 13个独立请求均completed、accepted、0 retry；D8 1个共享策略请求，D6/D7复用、0新增请求。共14次唯一请求，260,503 input（其中cached 139,392）+2,191 output=262,694 tokens；标准额度估值0.740981、API等值$0.02963924，实际账户实扣未知。
- v5唯一delta manifest SHA `ca9aa4083124b79914d02cff7477826d66559e99eb11e5f7717293c71a5a2b40`，覆盖新增D5/D6/D7三个source run、144段（72空驶/72载客）。公式与来源校验`audit_snapshots/time_money_v5_delta_audit_20261029T1241CST.json` SHA `300f349c5dee77da12c4377687b2a003ae64d018986d7161f3bf665dc14fff76`：`max(segment signed-net EV2 kWh,0)` mismatch 0，source hash mismatch 0、空驶收入错误0、乘客服务收入重复0。三项仍因缺电价、完整fare crosswalk、方法费率和共同候选路线池而blocked/null，0排序、0模型请求、0 SUMO；源结果未改。289项coverage index manifest SHA `6036e75e191c04f242a9fe260a2bdf5c98a5dceac0008a41028df36a5fd2f39f`。
- 截至12:42 CST，seed0继续在`EV_20240321_220000_q50/F4/D8_live`运行，尚未产生终态结果；本轮既有D5 F2及其D6/D7回放均已完成并进入T1240审计。
- 隔离legacy seed3 `EV_20241111_200000_q25/F2`的D8已完成（结果SHA `50fb6b310cb03b78143eb8c576d35f8af27938c284d3313a9c4da990301583e7`）；D6/D7 replay完成且无新增调用（结果SHA分别`6fa28ae08843356d3746cbfe6fd0e830a8302528af99b6d2d1145b3ef9b35044`、`80840f8bf53fe5451f14cdeabdc58347e89dcc6d3476263adda97a2d18ae4142`）。同格D5于12:41 CST启动live，12:42仍在进行；等待终态后单独审计legacy七法格及usage，不并入EV2 v4。


## 2026-10-29 12:46 CST 增量：legacy seed3 F2七法格完成

证据类型：legacy完整cell结果、共用场景/输入指纹、逐轮validator与原始Codex CLI usage复核。该实验使用dispatch-agent-v1旧schema，单独于EV2 v4 seed0。

- `EV_20241111_200000_q25/F2`七法均完整：D0/D1/D2/D5/D6/D7/D8分别490/490/490/489/490/490/490轮；每轮validator valid，硬约束错误和one-to-one错误均为0；七法使用同一scenario fingerprint `cfb2ee5288bc2286b8827c64032232a03df38a2a9b27fd7f7d8c8df75fb6937a`及相同的scenario/orders/zone-OD输入哈希。七法各服务24/393单。结果SHA依序为`3c4ebc818051a612dfbe285c2f346b48fb9890492e19b0d217ce444322416f08`、`e20de2c63b104314b58f0e70316c4ae1bf635935c7a8c6b17e847e0d8e6757df`、`eef3b2caaea9619178fb50e78df324d12a75cfe304a1291364ae339e0374294c`、`7fe984eb565a858629d12038060d94b3100b9a204cf8efc90f6d84a0d355aa2f`、`6fa28ae08843356d3746cbfe6fd0e830a8302528af99b6d2d1145b3ef9b35044`、`80840f8bf53fe5451f14cdeabdc58347e89dcc6d3476263adda97a2d18ae4142`、`50fb6b310cb03b78143eb8c576d35f8af27938c284d3313a9c4da990301583e7`。
- 唯一调用审计`audit_snapshots/isolated_legacy_full_cell_audit_EV_20241111_F2_seed3_20261029T1246CST.json` SHA-256 `8e327a2d08eab45335b8e7112bf5bc44d6ce40f9278280f9d2594e6a75be64f0`：D5 18次accepted/completed调用、0 retry；D8 1次共享策略请求，D6/D7仅复用该D8响应，没有新增调用。共19次唯一调用，316,508 tokens（314,350 input，其中cached 167,680；2,158 output）；标准额度估值0.88193、API等值$0.0352772，实际账户账单未知。该cell不是EV2 seed0结果，亦不支持整体性能排名。
- 截至12:46 CST，official seed0正在同场景F4的D6/D7无新增请求replay；legacy seed3已从该F2完整格进入`EV_20241111_200000_q25/F4/D0_D1`确定性基线。保持两批隔离，待下一组结果完成后再做对应审计。


## 2026-10-29 12:29 CST 增量：EV2 seed0审计扩展至286项

证据类型：EV2 v4正式fullmatrix审计、独立输入指纹、与GitHub 269项检查点逐run SHA-256比较。

- `ev2_fullmatrix_audit_20261029T1229CST.json` SHA-256 `fde185b1b834a267f163573621f89f777dd92508335f0021c22179d0b6d72ce7`（04:28:59Z）：286/630有效、344缺失、0无效。输入指纹审计`ev2_input_fingerprint_audit_20261029T1229CST.json` SHA `29044c304d4d5a1ff848a4193d5494fdf15fe4e32126b2312c3ed3c9be6904cd`（04:29:12Z）：286项全部匹配冻结输入和同格fingerprint，0 mismatch。D0/D1/D2各90，D5=3、D6=4、D7=4、D8=5。
- 对比GitHub当前269项快照，新增17项，原269项结果SHA全部不变。17项均完整有效、`partial_run=false`、invalid/hard-constraint error为0，且结果文件SHA与两份审计逐项吻合。新增单元为`EV_20240321_220000_q50/F1`的D5–D8和F2/D8、`EV_20241115_224000_q75`的F2/F4各D0–D2、以及`EV_20241222_205500_q25`的F1/F2各D0–D2。逐run校验和见本轮档案manifest与SHA256SUMS。
- 当前仍为286/630部分矩阵，不能据此给出总体方法排名。压缩包只包含新增17个结果，审计及增量包存于`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/repository_archives_20261029T1234CST/`。


## 2026-10-29 12:53 CST 增量：远端检查点对比与本轮归档

- GitHub EV2 seed0检查点为269项；T1240审计为289/630有效、341缺失、0无效，独立输入指纹全匹配。269个既有结果SHA全部不变，本轮新增20项：`EV_20240321_220000_q50`的F1/F2各D5–D8四法；`EV_20241115_224000_q75`的F2/F4各D0–D2三法；`EV_20241222_205500_q25`的F1/F2各D0–D2三法。结果压缩档保留每个源文件SHA和轮数。该快照仍是部分矩阵，不形成总体排名。
- 一并归档独立legacy seed3 F2七方法完整格及19次唯一调用审计、v5三个source-run/144段离线侧账，以及R4两条已终态任务的审计/usage和44路线全流程审计。legacy旧schema和EV2 v4分开保存；运行中的F4批次与R4父批次仍按进行中处理。
- 归档目录：`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/repository_archives_20261029T1253CST/`、`outputs/route_layer/repository_archives_20261029T1253CST/`。


## 2026-10-29 13:02 CST 更正：本轮GitHub归档采用T1302目录

- 上一节中的`repository_archives_20261029T1253CST`为构建前计划路径；实际未生成提交。当前构建的完整归档目录为`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/repository_archives_20261029T1302CST/`，R4配套档案为`outputs/route_layer/repository_archives_20261029T1302CST/`。
- 同步范围以T1240矩阵审计、T1246 legacy seed3 F2完整格和T1241 v5三run侧账为准；seed0与legacy仍分开统计。

## 2026-10-29 13:14 CST 增量：EV2 seed0新增两个七方法完整格

证据类型：seed0 EV2 v4全矩阵及独立输入指纹审计、逐轮validator、同格完整性、Codex CLI原始trace与usage、唯一调用去重审计、v5离线侧账公式复核。

- T1311正式矩阵快照 `audit_snapshots/ev2_fullmatrix_audit_20261029T1311CST.json` SHA-256 `7f70e298566544d0eb2a8fa4dddc5b9f06c36e226e907ae83afea3349ddd927b`：297/630项完整有效、333缺失、0无效；D0/D1/D2各90，D5为6，D6/D7/D8各7。独立指纹审计 `ev2_input_fingerprint_audit_20261029T1311CST.json` SHA `82a7fbede5a5210635c6cdd037dd1cf13ffbfcb00062f582a9e50a78a23c6df9`：297项冻结输入、方法契约、EV2 profile均匹配，场景/输入mismatch和逐项错误均为0。90个场景—车队格中6格七法齐备；矩阵仍不完整，不能给出总体排名。
- 新完成格 `EV_20240321_220000_q50/F4` 七法各96段，`EV_20240330_200500_q75/F1` 七法各26段。两格逐轮validator全部通过，invalid round、硬约束错误、一对一错误均为0；同格共享输入哈希。场景指纹分别为 `af87cc977c51920fc8808757c11dfaaab17d8f4d59ea57599a53ec735cc9eb43` 与 `878f96d711dbffa10b58c2ca7bcde7d2a77401b00fc50221a396b35d5548a510`。对应独立格审计SHA分别为 `971e0b283814406666a999c9d1c4b282ff6c582b1f41a864bf07d2312abed87d`、`446bd5b932b121abf9966ee8c81d3a884881e550dfdbe1e0b0bac379f20d618b`。
- F4格48/502单服务，D0/D1/D2/D5/D6/D7平均接驾等待为102.214秒（D5为102.234秒），D8为105.184秒；七法P95均为246.398秒。D0至D7模拟EV2总能耗均为35.976425 kWh，D8为36.009321 kWh。F1格13/565单服务，各法平均接驾等待127.706秒、P95为298秒、模拟EV2总能耗3.740675 kWh。两组只支持各自冻结输入上的描述比较，不外推总体效果。
- 结果文件SHA按D0/D1/D2/D5/D6/D7/D8顺序：F4 `55fccfb1c8b5d3da28cc5465ce17eb4b69ba9e7433165fce528d76cdb92a8fe5`、`f6fc234b0c643218a66cc48bcb4b2610867fe33af6095bbbf519518ed72979d6`、`5f521417ba9416c74b11d916a0de919082fa12910025e5142b8cb11124c1ab5d`、`5260dea8a70726aa771ccd1d3940af8b36d73514cb5eec6700cfd48abe80221b`、`a29aa2d440905621afb0881e85e30d164d274b215a51a944b9efc8de03ab3405`、`d4c19f9bf68fd44edca989243be2604899689ebf39750e536f44fba3c42b6a42`、`172c78e6edfe0cd267a0d8fd494767b37127373742474ded9b3e7746f33d28b6`；F1 `9b23073560ebe0ec9bbe7b9b5fbb9f7aa2b74e6b228ce91aed73a3dddc0ac7f3`、`bec51c350e92b1932378a431bf5a972db31817d7f3b632479a8974683303c09d`、`f77ddf900abfcd31ff607e84796902c5ac5fc00804df2662518faa30fbc8ff59`、`8f5c5dc28cfc7e1f7463f1f5a1bff8401d1a8a6448a225c3ea5124506e7c8265`、`608553f16e7704cd80da42977563359cf444aa3c032a22fd679eb58f7aa3be04`、`f8950f10b6f2a071f7caf523270e2a7072bec040266016074bfdab80d4a0a9cf`、`687873fd0bc2275e25a6c4124c13ad7536faa32c82d423169d24d954f254f997`。
- F4唯一调用审计 `unique_invocation_audit_EV_20240321_F4_20261029T1308CST.json` SHA `755561ee054f1a1e0507643c4f5b4dd91bd791f5e91f7757ee8ab62db3e70859`：D5 28次响应均accepted/completed，D8 1次accepted；D6/D7重放同一D8响应，没有新调用。共29次唯一调用，659,159 input（其中cached 316,800）、4,403 output、663,562 tokens；额度估值2.002285 credits、API等值估算$0.0800914，实际账单未知。F1唯一调用审计 `unique_invocation_audit_EV_20240330_F1_20261029T1312CST.json` SHA `e59d52b03247cbb1c8457ecf4603639ff81cb1b3616e647f8d1ec96235b09298`：D5 7次accepted/completed、D8 1次accepted，D6/D7为零新调用回放。共8次唯一调用，147,955 input（cached 101,376）、1,358 output、149,313 tokens；额度估值0.324323 credits、API等值估算$0.01297292，实际账单未知。两个格都没有失败/未知调用或重试。D0不优化能耗；D5虽接收EV2信息但没有确定性全局能耗优化；D1/D2/D6/D7/D8按冻结词典序把EV2 signed-net总能耗作为末级目标。
- v5离线侧账继续按批次去重：T1306增量3个源结果/148段（D5 F4、D6与D8 F1），公式审计SHA `47b0baf9dc0055c7e3d2cd09007ed62383b52e5830a6da5f4f2175eb99340184`；T1312增量2个源结果/52段（D5与D7 F1），公式审计SHA `512593498875da47b5e7ac4d328958a381a73d390a8165fe2007779da478e861`。两批逐段 `max(signed_net_kwh,0)` 最大差均为0，源SHA和收入语义核验通过，未改结果、未调用模型、未重跑SUMO。缺冻结电价、完整票价交叉表、方法计算费率及完整共同候选池，所有评分保持blocked/null，不给利润排名。新覆盖索引 `time_money_v5_source_coverage_index_20261029T1312CST_297/manifest.json` SHA `2be2a07edc6e52fcfcd9fdaded2f0d1ce26b5cd4543b790366e49376002e2e4a`。
- 研究边界：NYC订单是TLC真实记录映射为zone-level OD成本代理，接驾是质心代理；不是街道级路网、真实ETA或实车遥测。seed0全矩阵内其他12个D5 HTTP 429历史失败保持failed，不重试。本快照后正式seed0已进入 `EV_20240330_200500_q75/F2` 的D8完成、D6/D7回放运行；legacy seed3 `EV_20241111_200000_q25/F4/D5`仍在单独执行，未与EV2混算。




## 2026-10-29 13:19 CST 增量：NYC EV2 seed0更新至297项与新增完整单元

证据类型：已完成实验（NYC EV2 v4 seed0的单元与逐项审计）；进行中状态快照（R4 formal父manifest与worker）。

- NYC EV2 seed0最新全矩阵审计`ev2_fullmatrix_audit_20261029T1311CST.json`源SHA-256 `7f70e298566544d0eb2a8fa4dddc5b9f06c36e226e907ae83afea3349ddd927b`：297/630有效、333缺失、0无效。输入指纹审计SHA `82a7fbede5a5210635c6cdd037dd1cf13ffbfcb00062f582a9e50a78a23c6df9`：297项冻结输入/同格指纹/契约mismatch均为0。与T1240的289项对比，8个结果新增、既有结果哈希无变化、无审计结果被移除；90个场景—车队格中6格已具备七方法结果，矩阵仍不完整，不支持总体排名。
- 新完整格`EV_20240321_220000_q50/F4`由`full_cell_audit_EV_20240321_F4_seed0_20261029T1308CST.json`（SHA `971e0b283814406666a999c9d1c4b282ff6c582b1f41a864bf07d2312abed87d`）核验；`EV_20240330_200500_q75/F1`由`full_cell_audit_EV_20240330_F1_seed0_20261029T1312CST.json`（SHA `446bd5b932b121abf9966ee8c81d3a884881e550dfdbe1e0b0bac379f20d618b`）核验。两格七法均完成，seed/input一致，逐轮validator通过，硬约束、一对一及commitment违规均为0。仅是两组配对单元的描述性证据，不外推到全矩阵。对应唯一调用审计分别为29次/8次，0 retry；D6/D7离线复用D8共享响应，不重复计模型调用。
- v5增量审计`time_money_v5_delta_audit_20261029T1312CST.json`源SHA `512593498875da47b5e7ac4d328958a381a73d390a8165fe2007779da478e861`：新增2个source runs、52段；公式/来源检查通过，但2项仍因缺少评分所需的共同输入而blocked/null；无排名、无模型调用、无SUMO重跑，源实验结果未改。最新297项coverage index仅为离线数据覆盖表，不是决策结果。
- 隔离legacy seed3的`EV_20241111_200000_q25/F4`部分审计SHA `79ba08c0191939d717d52de14fe4859187f1715f811b097030684b37c8ecadbd`核验D0/D1/D2/D8四项均完整、同输入与同scenario，另三方法尚缺；明确不具备完整七法对比资格，也不并入EV2 v4。

- 安全归档候选为`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/repository_archives_20261029T1319CST/`和`outputs/route_layer/repository_archives_20261029T1319CST/`；本轮旧T1302暂存包尚未推送，改由T1319候选包承接此前待同步内容及本次新增结果。




## 2026-10-29 13:25 CST 更正：EV2输入指纹暂超出完整矩阵审计

证据类型：进行中进度审计，不是实验结果终态。

- 输入指纹审计`ev2_input_fingerprint_audit_20261029T1321CST.json`源SHA-256 `1c173fca9b788dbf23238abd7f3e9e805c03023ff92b1125e5d85b1eea817b72`记录300项，冻结输入/同格指纹mismatch为0；但截至13:25，最新完整矩阵审计仍是T1311的297项。两份审计尚未对齐，新增3项只作为待复核进度，不计入297项正式矩阵，不复制这些结果，也不做效果判断。
- T1323 v5侧账依赖上述未对齐的新输入集合，本轮不纳入正式归档；等待新fullmatrix与相应单元审计后再处理。活动trace、progress日志和raw prompt/state不归档。
- 13:19候选目录尚未推送；当前归档候选改为`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/repository_archives_20261029T1325CST/`和`outputs/route_layer/repository_archives_20261029T1325CST/`，包含T1311已匹配的297项审计快照及单独标明的T1321进度审计。

## 2026-10-29 13:30 CST 增量：EV2 F2七法完整格、seed0更新至301项与legacy F4完整格

证据类型：EV2官方逐项/冻结输入审计、同格七法审计、Codex CLI原始trace与去重usage；离线v5公式侧账；legacy seed3隔离完整格审计。

- EV2 seed0 T1326矩阵账本 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_ev2_segment_ledger_v4_20261027/seed_0/audit_snapshots/ev2_segment_ledger_audit_20261029T1326CST.json` SHA-256 `c26f52a78f3691580970cbfd5efde515d45c1f9b0aec45ca1b3ce2ca2ce1d654`：301/630项完整有效、329缺失、0无效；D0/D1/D2各90、D5为7、D6/D7/D8各8。90个case—fleet单元中7格七法齐备、83格部分完成。12个历史D5 HTTP 429仍记failed，无重试；全矩阵不完整，不能报告总体方法排名。T1327指纹审计 `ev2_input_fingerprint_audit_20261029T1327CST.json` SHA `310eacbd494c7698e0ff7e8cce9b315055b4aa605d4ef3f41e22367280dead0c`：301项全部匹配冻结输入、同格场景指纹、方法契约与EV profile；cell mismatch、单项错误均为0，profile SHA均为 `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。
- 新完整格 `EV_20240330_200500_q75/F2/seed0` 全格审计 `full_cell_audit_EV_20240330_F2_seed0_20261029T1328CST.json` SHA `04e1aed20f166dc8a2d12cf17b8e82bc6b377b5fe823b319593fc3e53d0e7280`；scenario fingerprint `3616eee051f6c492a30dd1d407e403f3f991725e4c19793465553c3c5ab894d6`。7法均完整、同seed/输入，逐轮validator通过，硬约束、一对一和commitment错误为0。各法均服务27/421单（D5为26/421）；平均等待D2/D6为108.202秒、D5为131.065秒、其余为116.110秒。该单元仅支持本格冻结输入下的描述比较。
- F2唯一调用审计 `unique_invocation_audit_EV_20240330_F2_20261029T1328CST.json` SHA `c6f2ae3d208fd9cd9465307fd4e90d7819770003903f2032c890e4ed033ff0ab`：D5 12次、D8共享策略1次，D6/D7复放同一D8策略、没有新增调用。共13次唯一调用，281,516 input（cached 152,064）+2,474 output=283,990 tokens；额度估值0.797512 credits、API等值估算$0.03190048；0重试、0失败/未知，真实账户扣费未知。
- v5离线侧账增量按源hash去重：T1323新增F2 D6/D7/D8 3个source runs、162段；公式审计SHA `298525f8e84f1604cbe468d66e2c25508694857be8f9ff56a0635ec1ce86f523`，最大 `max(signed_net_kwh,0)` 差0。T1330新增F2 D5 1个source run、52段；公式审计SHA `bfc24028270cd228051cbfbb72032fae1b3983a0e30fb03e5cbc88d8f6e7732b`，最大差0。累计301源结果覆盖索引 `time_money_v5_source_coverage_index_20261029T1330CST_301/manifest.json` SHA `f9c46df99de5c862009b39483eaaa60988b566b5ffb31032d68cba17780ba1e4`。侧账保留乘客票价语义、空驶收入为0、方法计算成本按run只计一次；没有冻结电价、完整hash票价交叉表、方法计算费率和完整共同候选池，故利润/route-choice仍blocked/null、0排名。无模型调用、无SUMO重跑，源结果未改。
- 隔离legacy `dispatch-agent-v1` seed3单元 `EV_20241111_200000_q25/F4` 七法完整审计 `outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_replicates_v1/seed_3/audit_snapshots/isolated_legacy_full_cell_audit_EV_20241111_F4_seed3_20261029T1318CST.json` SHA `7ef965124f281261567367684c50f1344d96cdd2c6465d3bbd7c3c59e994fcdb`：同scenario/input fingerprint，逐轮validator、硬约束、一车一单及commitment检查通过；各法48/393单。平均接驾等待D0/D1/D2/D6/D7为116.325秒、D5为114.441秒、D8为114.420秒。唯一调用35次（D5 34、D8 1，D6/D7共用回放），682,888 tokens，额度估值1.701797/API等值$0.06807188，0重试/失败/未知，实际账单未知。该结果属于legacy seed3，不与EV2 seed0合并或参加EV2排名。
- 本次检查时，EV2官方runner已开始同日期F4/D8 live，seed3 legacy runner已进入下一格 `EV_20241107_214500_q50/F2` 的D0/D1；两者仍在运行，不把进行中单元写作完成。TLC需求仍是映射到zone-level OD成本代理，接驾为合成质心代理，不是街道级路线或车辆遥测。完整结果SHA、trace和时间戳见对应审计JSON。




## 2026-10-29 13:30 CST 更正：EV2 segment-ledger审计与指纹已对齐至301项

证据类型：已完成实验（官方EV2 segment-ledger审计、输入指纹、单元审计与离线v5侧账）；进行中状态快照（R4 formal父manifest与worker）。

- 较新的官方`ev2_segment_ledger_audit_20261029T1320CST.json` SHA `6f21bf1315a915cc53d2ea0026d124576ca8cf3c8a76c71f380d6a46c0745e3f`审计300/630项，并由T1321指纹审计SHA `1c173fca9b788dbf23238abd7f3e9e805c03023ff92b1125e5d85b1eea817b72`逐项匹配。随后T1326 segment-ledger审计SHA `c26f52a78f3691580970cbfd5efde515d45c1f9b0aec45ca1b3ce2ca2ce1d654`更新为301/630有效、329缺失、0无效；T1327指纹审计SHA `310eacbd494c7698e0ff7e8cce9b315055b4aa605d4ef3f41e22367280dead0c`的301项输入/场景mismatch均为0。相对T1240的289项，新增12项、既有哈希0变化、0移除；90个场景—车队格中7格具备完整七方法结果。整体矩阵仍不完整，不支持总体方法排名。此前T1311常规fullmatrix审计297项被后续官方segment-ledger审计 supersede。
- 新完整格`EV_20240330_200500_q75/F2`由T1328单元审计SHA `04e1aed20f166dc8a2d12cf17b8e82bc6b377b5fe823b319593fc3e53d0e7280`核验七法全部完成、输入/seed匹配、validator通过，硬约束、一对一与commitment违规为0。该格唯一调用审计记录13次调用、0 retry。它是单格描述证据，不代表总体比较。
- T1323 v5侧账覆盖3个source runs/162段；T1330侧账覆盖1个source run/52段。两份公式审计均无错误、源结果哈希匹配，source结果未改；缺少冻结费率/共同决策输入，route choice与盈利未评分、无排序、0新模型调用、0 SUMO。T1330的301项coverage index是离线覆盖数据，不是方法决策结果。

## 2026-10-29 13:40 CST 增量：EV2 seed0审计到302项，新增D8单方法单元

证据类型：已完成EV2 v4单方法运行、segment-ledger与输入指纹审计、Codex CLI trace/usage核验、v5离线sidecar公式复核；D6/D7 replay仍在进行。

- T1335官方segment-ledger审计 `audit_snapshots/ev2_segment_ledger_audit_20261029T1335CST.json` SHA-256 `5243ca5966e63b519722f1ff383455104cae99bec0555ccda4a5292c5b9b92cc`：302/630有效、328缺失、0无效。D0/D1/D2各90，D5=7、D6/D7=8、D8=9。输入指纹审计 `ev2_input_fingerprint_audit_20261029T1335CST.json` SHA `fee6920774776f58e8e71e4d8c9512e3b37831fff5364d4e492678bf75f683db`：302项冻结输入、同格场景与方法契约均匹配，mismatch和逐项错误为0；EV2 profile SHA均为 `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。90格中仍只有7个完整七方法格，矩阵不完整，不支持总体排名。
- 新完成的单方法结果 `EV_20240330_200500_q75/F4/D8` SHA `7a1045cf0a704728d44c8e0edcaa458cd403dae6e93e9dab7fdf1644cc53f41e`：完整仿真531轮，逐轮validator错误0，服务53/565单；模拟EV2能耗25.863815 kWh。该格T1335只有D0/D1/D2/D8四项结果，D5缺失；D6/D7共享D8策略的无模型回放正在运行，因此该格目前不构成七方法对比。
- D8本单唯一调用1次：13,451 input（12,672 cached）+199 output=13,650 tokens，0失败/未知/重试；额度估值0.016201 credits/API等值$0.00064804，实际账户扣费未知。矩阵累计D5 trace有12个HTTP 429失败，继续保留failed、不重试。
- 冻结决策契约抽查确认：D0的派单规则不使用EV能耗；D5只向LLM提供pickup/ride EV2 signed-net信息，不套确定性全局能耗目标；D1/D2/D6/D7/D8的EV2 signed-net能耗均为词典序末级目标。NYC订单仍是TLC真实需求映射为zone-level OD代理，接驾使用合成质心代理，不是街道级路网或车辆遥测。
- v5离线侧账新增该D8源结果1项/106段。formula audit `time_money_v5_delta_audit_20261029T1335CST.json` SHA `6e9c7defd0a8c3301bf4d98b693ceab3c5f081a5c221ef6ca1940543b264bf24`：逐段 `max(signed_net_kwh,0)` 最大差0，乘客票价只记一次、空驶收入为0、source SHA匹配且源文件未改。因缺冻结电价、完整fare crosswalk、方法计算费率及完整共同候选池，route choice/利润仍blocked/null、0排序、0新请求、0 SUMO。302项coverage index manifest SHA `259420ab7a5eb98dfc8e0b9defa095970aa341694f5c9f727ef5410e767a309b`。
- 截至13:40 CST，seed0 `EV_20240330_200500_q75/F4` 的D6/D7确定性回放仍在运行，无新模型调用；D6结果文件已出现但尚未纳入T1335审计，D7结果文件尚未出现。legacy seed3已完成下一单D8并进入D6/D7回放，继续与EV2 v4分开核验。

## 2026-10-29 14:00 CST 增量：seed0新增两个七方法完整格

证据类型：EV2 v4正式账本和输入指纹审计、逐轮validator、完成格唯一Codex调用审计、v5离线公式侧账。仅记录单格描述性结果，不形成总体方法排名。

- 官方账本审计 `audit_snapshots/ev2_segment_ledger_audit_20261029T140023CST_live.json` SHA `fe1bedcbde9b2cf5b21273f28005c84bee26e8446c7662f4bab263ef885d5fe2`：309/630有效、321缺失、0无效；D0/D1/D2各90，D5=9，D6/D7/D8各10。输入指纹审计SHA `088ec79697dcd725179caf9fee4c08879c7b3870312be76fde9f87dd975cc12d`：309项均与冻结输入、同格指纹和方法契约匹配，scenario/input mismatch与逐项错误均为0；EV2 profile SHA均为 `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。七方法完整格增至9/90，矩阵仍未完成。
- 新完整格为`EV_20240330_200500_q75/F4`和`EV_20240401_200000_q25/F1`。两格各7法齐全、共享输入哈希、scenario mismatch为0，每法逐轮validator错误/无效轮数均为0。scenario SHA分别为`237684af98b0b570b69e1247288ef68ff62f086a0c9a7c24746363741fef2bbf`和`ccee7c9cf4372e6599f10e3a29aba8f5a463b517d59ac87d2c187780df93b0d6`。
- 去重调用审计 `unique_invocation_audit_EV2_seed0_two_cells_20261029T1400CST.json` SHA `b935dbae4423aa38e8adf0aad4ea3873fa06a942d6c95f640fd08728ebe80fa3`：无失败、未知、429、重试或usage缺口。F4共29次唯一调用（D5 28次，D8 1次；D6/D7重放D8同一响应），770,132 input（354,816 cached）+4,724 output；估值2.395708 credits/API等值$0.09582832，实际账户扣费未知。D5服务53/565单，平均接驾113.403秒、P95 299秒，EV2模拟能耗26.2521715 kWh；D5结果SHA `4dc8a0d0930c616fdd1d2e7f8098c3a9f9dc63e5ab555bdb32ca6887a29da49f`。
- F1共10次唯一调用（D5 9次、D8 1次；D6/D7复用D8响应），170,877 input（101,376 cached）+1,171 output；估值0.433323 credits/API等值$0.01733292，实际扣费未知。D5服务13/433单，平均接驾158.414秒、P95 297.770秒，EV2模拟能耗10.2938344 kWh；D5结果SHA `d8a08013668418f6dcf70afcc02967f89651c51b1b37482299658d65056ab9d1`。
- v5离线侧账对这两格新增5个源结果/210段。公式审计 `time_money_v5_delta_audit_20261029T1400CST_corrected.json` SHA `8594de77d00c42770c1890c0bf6e4e1d87cf7059fc75d91a10f8013fb73ab54b`：按`max(signed_net_kwh,0)`核验、乘客票价逐段保留、空驶收入为0、源SHA匹配且源结果未修改，0错误。首份校验曾因把重复的candidate_id当唯一段键产生误报，已保留为`...failed_join_key.json`并由按“段序号+candidate ID”审计的本版本更正。因冻结电价、完整票价映射、方法计算费率与共同候选池缺失，route-choice/利润保持blocked/null，0排名、0模型调用、0 SUMO。覆盖309个源结果的index manifest SHA `721bb5be3069faca2d82066fe1a529d679ca2a1bd899bcf725d5deb7e6497951`。
- NYC仍是TLC真实订单需求映射到zone-level OD成本代理、质心接驾代理，不是街道级路网或实车遥测。D0不以能耗优化；D5虽接收EV2信息，但没有确定性全局能耗目标；D1/D2/D6/D7/D8仍按冻结词典序将EV2 signed-net能耗作为末级目标。上述两格不能外推为整体优劣。

## 2026-10-29 14:10 CST 增量：EV2 seed0 F2新增完整格

证据类型：官方EV2账本与冻结输入指纹审计、F2完整格逐轮validator、去重Codex调用审计及v5离线公式复核。只作该格描述性记录。

- T1410账本 `audit_snapshots/ev2_segment_ledger_audit_20261029T141012CST_live.json` SHA `a3f3f095374779b0c0a4d4a957997cbc3243e3b7de594071130a4fea77da90dd`：313/630有效、317缺失、0无效；D0/D1/D2各90，D5=10、D6/D7/D8各11。指纹审计SHA `430f6fac06f7a27de894f9c51a03ee5da899c105ab9c7e628b7371d47a25cee0`：313项冻结输入与方法契约匹配，0场景/输入mismatch和逐项错误；profile SHA统一为 `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。完整格增至10/90，全矩阵仍未完成。
- 新完整格 `EV_20240401_200000_q25/F2` 七方法均完整、共享输入、逐轮validator错误/无效轮数为0；scenario fingerprint `fb51ff9bf19342af614b272d2c74903207fb0262ed6d2cab547255175f294dd3`。D5完成486个派单轮次，服务25/433单，平均接驾151.972秒、P95 297.770秒，模拟EV2 17.802484 kWh；D5结果SHA `686be61ab8fb6409b92f37071dc731a128d71cd6e3f1892d0e647f81b2f45a65`。
- 唯一调用审计 `unique_invocation_audit_EV_20240401_F2_20261029T1410CST.json` SHA `d368089ae75ef63549a6d4b3d0be37252c54dce03d7052338b4452b338538ad9`：21次唯一调用（D5 20次、D8 1次；D6/D7复用D8响应），378,221 input（240,768 cached）+2,225 output，总380,446；估值0.874399 credits/API等值$0.03497596，实际扣费未知。trace无失败、429、重试、格式错误或usage不全。
- v5离线侧账新增D5一个源结果/50段。公式审计SHA `027e28d17305f63f50e46b726628bab2a57b679c0804edb63a39f752758a6d1c`：`max(signed_net_kwh,0)`、乘客票价与空驶零收入规则均通过，0错误；源SHA匹配、源结果未修改。缺冻结电价、完整票价映射、方法费率和共同路线池，route-choice/利润保持blocked/null，0排名、0模型调用、0 SUMO。313项覆盖index manifest SHA `7a671920df430eb00812bf93ba83f2fb230092c64433ad81250179c4015d189a`。
- NYC仍为TLC真实订单需求映射到zone-level OD成本代理和合成质心接驾代理，不是街道级路线或实车遥测。D0无能耗目标，D5无确定性全局能耗优化，D1/D2/D6/D7/D8将EV2 signed-net作为词典序末级目标。单格结果不能外推总体排名。

## 2026-10-29 13:49 CST 更正增量：F4 D6/D7回放完成并审计通过

证据类型：EV2 v4全矩阵账本/输入指纹、D6/D7逐轮validator、共享Codex调用去重及D7 v5离线侧账；不把活动中的D5写为完成结果。

- T1346全矩阵审计 `audit_snapshots/ev2_segment_ledger_audit_20261029T1346CST.json` SHA `3ac9f71f4cf4f9be4f2f5acb652e7f31ea2d7686ec1b3587efaa43d903df0f21`：304/630有效、326缺失、0无效；D0/D1/D2各90，D5=7、D6=9、D7=9、D8=9，七方法齐备格仍为7/90。指纹审计SHA `5d5e3187dd8b985cf838f74e95d70e8ffdcdecd26a98b6baf9244e51f1a9aa13`：304项冻结输入/场景/方法契约匹配、0 mismatch、0错误；profile SHA均为 `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。全矩阵仍不完整，不报告总体排名。
- `EV_20240330_200500_q75/F4` 的D6、D7回放各已生成完整531轮结果，逐轮validator错误0；T1346单元有D0/D1/D2/D6/D7/D8六法，D5结果当时缺失，故仍非完整七法对照。seed0 D5历史HTTP 429仍保留failed且没有重试。
- D6/D7分别沿用D8共享策略trace，三个方法结果的trace路径/SHA相同。去重审计 `unique_invocation_audit_EV_20240330_F4_20261029T1346CST_partial.json` SHA `ba65c7e6f66cba6dad3aef2d834cf6c179d9b46b34d55d892475834a9a26fc68`：D6/D7/D8合计仅1次唯一调用，13,451 input（12,672 cached）+199 output=13,650 tokens，0失败/未知/重试；估算0.016201 credits/API等值$0.00064804，实际扣费未知。该账本不覆盖缺失的D5，因此不是完整cell调用审计。
- v5侧账新增D7结果1项/106段。公式审计SHA `de95d3d8c259d5f99f8cfddc7ba5304bd3dfd8dd2d1042b990b2305ef47ce3c5`：逐段 `max(signed_net_kwh,0)`差0，乘客收入保留一次、空驶收入0、source SHA匹配且源结果未修改。缺冻结电价、完整fare crosswalk、方法计算费率和共同路线池，route choice/利润仍blocked/null、0排名、0模型请求、0 SUMO。304项覆盖索引manifest SHA `da68608ed869c197caff63733b0a4828503164fd2c6b79489e68e59679763420`。


## 2026-10-29 14:21 CST 增量：F4 D6已审计，D8策略被拒，D7回放在途

证据类型：官方EV2 segment-ledger与输入指纹审计、已完成D6/D8方法结果及其逐轮validator/原始Codex trace；同格仍不完整。

- 官方账本 `audit_snapshots/ev2_segment_ledger_audit_20261029T0620UTC_live.json` SHA `d901dfdedfbe7bf510e508485dc1d5f5d581ff6837c7364b9e9d54c343d3ffcb`：315/630方法结果有效、315缺失、0无效；D0/D1/D2各90，D5=10、D6=12、D7=11、D8=12。指纹审计 `ev2_input_fingerprint_audit_20261029T0620UTC_live.json` SHA `47b10abed8f490c358f1842a3ed94f521e3f0389467a696c0566d2d67e71c4e3`核对315项，输入/场景mismatch=0、逐项错误=0，EV2 profile SHA仍统一为 `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。七法完整格仍为10/90；矩阵不足以支持总体排名。
- `EV_20240401_200000_q25/F4/D6`结果SHA `3cb9909e48448a64edbb1cf7bc3683a110d00758d25aa137f51cc9141cbfcd50`：510轮、510轮validator均valid、硬约束错误0；唯一Codex策略被接受。服务50/433单，平均接驾121.983秒、P95 294秒，模拟EV2 billable能耗32.499233 kWh。该次trace SHA `6ab208018cf54403fe5ad3ea8c7fedfaddbdf7769ae3633611dfc7167519e4fb`记录13,453 input +218 output=13,671 tokens，额度估值0.073805 credits，实际账户扣费未知。
- 同格D8结果SHA `af38a8afa87261f7b9e1b13ca2318057fcdb9eff29ecece053473b5816bcc863`是512轮完整fallback仿真，512轮validator均valid；但唯一Codex响应状态为 `invalid_policy_parameters` / `policy_values_out_of_bounds`，因此不是被接受的D8 LLM策略结果。trace SHA `3ac16e7c22035f62a4ae47e4e2439ba8acb22cb9387f098346426f4360133ab4`记录13,451 input +262 output=13,713 tokens，额度估值0.075115 credits；请求已正常完成，不标为投递未知或重试。
- 批次在06:18:39 UTC启动D7对已接受D6策略的确定性回放；截至06:21:39 UTC仍在运行，尚无最终结果。D5在该F4格仍缺失，故本格没有七方法完整比较。侧车不扩为在线selector、不产生利润或路线排名；目前v5侧账仍是独立离线核算。


## 2026-10-29 14:30 CST 增量：F4 D7回放完成，完整格仍缺D5

证据类型：官方EV2 segment-ledger与输入指纹审计、D7结果逐轮validator及D6策略回放标记；本格仍非完整七方法对照。

- T0630官方账本SHA `3f0a81eb4d97c2a953580587d3b45b0a33802e1ca07b929aaeede6a4a53e89ae`：316/630方法结果有效、314缺失、0无效；D0/D1/D2各90，D5=10、D6/D7/D8各12。指纹审计SHA `15169d35e03934cd6406e858fb6203c327a7308e5d19395870b0120f8c90c1cf`：316项输入/场景不匹配=0，错误=0，profile SHA不变。完整七法格仍10/90。
- `EV_20240401_200000_q25/F4/D7`结果SHA `024edf3333b647d276bf924077939ed45a6a80f4f7cbc51eb7229909b9b8f6ac`：512轮完整、512轮validator均valid、硬约束错误0；服务50/433单，平均接驾127.440秒，模拟EV2 billable能耗32.620412 kWh。D7策略记录明确 `replayed_from_existing_call=true`、`new_invocation_made=false`，复用D6已接受响应；该格D6/D7只计一条唯一Codex调用，13,453 input +218 output tokens，0.073805 credits估值、实际账户扣费未知。
- T0630账本中D5仍缺，故本格无七法完整比较。D6/D7/D8结果通过同一输入指纹，但D8模型策略被拒的限制沿用14:21条目。v5离线侧账不生成路线选择或利润排序。

## 2026-10-29 14:42 CST 增量：EV2 seed0已审计320项，F1新格继续运行

证据类型：官方EV2 segment-ledger审计、冻结输入/场景指纹审计、逐轮validator与Codex CLI原始trace；v5离线sidecar覆盖索引状态快照。

- T0640官方segment-ledger审计 `audit_snapshots/ev2_segment_ledger_audit_20261029T0640UTC_live.json` SHA `21932f82ecf3f0c206fc6eb9863f88c9a4dfa87f8dfc946bce049b9257ac14b5`：320/630项有效、310项缺失、0项无效；D0/D1/D2各90，D5=11，D6/D7/D8各13。90个case—fleet格中11格均有七种方法输出；此计数不表示每格的LLM策略都被接受，也不支持总体方法排名。T0640输入指纹审计SHA `574978b173c302c1b0e39f6bbed6c9a2d3b27e3a6d8e157933445417d90878d1`（见审计文件）核验320项，场景/输入/契约错误和同格不一致均为0；EV2 profile SHA仍为 `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。
- 新完成单元 `EV_20240401_200000_q25/F4/D5` 结果SHA `540dad1623b0e760bd99f6332b3c47625d5c9ad81c44ac119d7c810add2c37a0`：513轮、逐轮validator均valid，100个EV2执行段、能耗与segment ledger相差约1e-14 kWh；服务50/433单，平均接驾127.480秒，模拟EV2 billable能耗32.620412 kWh。36/36条CLI trace均有完成usage，817,751 input（含443,520 cached）+4,393 output=822,144 tokens；额度估值2.224705 credits（费率等值$0.0889882），实际账户扣费未知；该格D5请求没有trace错误、429或CLI retry-limit事件。D5按冻结契约直接使用LLM派单，未施加确定性全局能耗优化。
- F4由此有七种方法输出，但该格D8唯一Codex策略仍是 `invalid_policy_parameters`；对应完整fallback仿真不能算D8被接受的策略结果。新格 `EV_20240411_200000_q50/F1` 已完成D8/D6/D7三项：D8策略accepted，D6和D7均复用该同一策略、`new_invocation_made=false`；三项各531轮validator通过、能耗9.327244 kWh。唯一调用13,449 input+197 output=13,646 tokens，额度估值0.073155 credits。该格D5已于06:39 UTC启动，T0640账本生成时仍在运行，未纳入完成项或七方法格。
- 最新v5侧账源覆盖索引 `time_money_v5_source_coverage_index_20261029T1410CST_313/manifest.json` SHA `7a671920df430eb00812bf93ba83f2fb230092c64433ad81250179c4015d189a`覆盖313个源结果；对T0640的320项逐文件SHA比较，7项新完成结果尚未进入该索引，无额外或哈希冲突项。这是索引时间差，不代表源结果被修改。T1410侧账仍为离线计量；冻结电价、完整票价交叉表、方法计算费率和共同候选池不齐，利润/路线选择继续null或blocked，不排名。此处未重算侧账、未发模型请求、未重跑SUMO。
- 隔离legacy seed3单独运行；其最新进度为 `EV_20241115_224000_q75/F1` 的D8已完成、D6/D7正在回放，尚未纳入隔离全格审计，不并入EV2 seed0。整体EV2矩阵仍不完整。

## 2026-10-29 14:44 CST 增量：F1七法结果齐备，F2 D8在途

证据类型：官方EV2 segment-ledger与冻结输入指纹审计、F1/D5逐轮validator和原始Codex CLI trace；seed0/seed3 progress时间点快照。

- T0642官方审计 `audit_snapshots/ev2_segment_ledger_audit_20261029T0642UTC_live.json` SHA `6137d794ef449ad2237c5b926c0ce77246d414fb11d4e858f66c66afd1fc842a`：321/630项有效、309项缺失、0项无效；D0/D1/D2各90，D5=12，D6/D7/D8各13。完整七法输出格增至12/90。T0642输入指纹审计SHA `b0eea40dac08aa463a456e2c4b3411e0a8ceeedfbf363816b83ac278e5e79250`覆盖321项，0项输入/场景/契约错误、0同格指纹不一致，EV2 profile SHA保持不变。
- `EV_20240411_200000_q50/F1/D5`结果SHA `47a7ccbe72053b9dd126a516440e95ae510ce05e192424ffad6540ed36dc9883`：532轮完整、validator无无效轮、24个执行段能耗与汇总一致；服务12/540单，平均接驾86.023秒、P95 184.862秒、模拟EV2 billable能耗9.141193 kWh。6/6 CLI trace完成且usage齐全：101,304 input（63,360 cached）+984 output=102,288 tokens；估值0.250920 credits、费率等值$0.0100368，实际账户扣费未知；trace中CLI错误、429、retry-limit均为0。
- `EV_20240411_200000_q50/F1`现有七法输出齐全，D8策略accepted，D6/D7复用同一策略且没有新增调用；D5是独立6次调用。该结果仅是一格seed0描述证据，不能据此得出总体优劣或排名。T0642之后runner已启动同case的F2/D8 live；最新progress到06:43:50 UTC仍在运行。
- 隔离legacy seed3的`EV_20241115_224000_q75/F1`于06:43:18 UTC完成D6/D7策略回放并启动D5 live；没有纳入新的隔离完整格审计，且与EV2 seed0分开。v5 sidecar索引仍覆盖313项，相对T0642审计的321项有8个新源结果未计入；保留既有侧账快照，不为覆盖差额额外请求或重跑SUMO。整体矩阵仍有309项缺失，不作总体方法排名。

## 2026-10-29 14:57 CST 增量：F2回放审计与legacy seed3新完整格

证据类型：T0654官方EV2账本及冻结输入指纹审计、F2/D8/D6/D7逐轮validator与策略复放标记、legacy seed3逐方法审计和原始trace；当前D5进度快照。

- T0654账本SHA `33e9169dfba6c3944c281fbd1122afaeab00fbe870cf6f3eca2839895cd1eade`：324/630有效、306缺失、0无效；D0/D1/D2各90、D5=12、D6/D7/D8各14。完整七方法输出格仍12/90。指纹审计SHA `7c26637847a75adce0b93304092359fac594be7fff4634b8a6e0e9d27ac965ff`核验324项，scenario/input mismatch与逐项错误均为0，EV2 profile SHA不变。
- `EV_20240411_200000_q50/F2`的D8/D6/D7已全部完成且逐轮validator有效。D8结果SHA `04f2900b95d23e02ecff80ac511b7d4ece91d0801942cee47f5f133893d5a50e`接受了一次策略，545轮、服务24/540单、平均接驾93.819秒、P95 246.398秒、EV2 billable 17.630994 kWh；唯一调用13,451 input+283 output=13,734 tokens，估值0.075745 credits，实际账单未知。D6 SHA `a54e1640f3ddfe25842a15c7e6b41cfaab35c28d04859ffc5e03247ba37e75c6`和D7 SHA `565fab176218a3a3815861d5d982e6cc2405b7a9c9566728965a08e2e0f63957`各544轮、无validator错误，均复用D8响应，`new_invocation_made=false`，没有新调用。D6/D7能耗17.599535 kWh与D8不同，按方法分别保留，不把复放解释为重复D8结果。
- 截至06:56:59 UTC，同格D5仍在运行；未计为完成。D5结束前F2不是七法完整格，矩阵总体仍不支持排名。
- 隔离legacy `dispatch-agent-v1` seed3的新完整格 `EV_20241115_224000_q75/F1`审计文件 `audit_snapshots/isolated_legacy_full_cell_audit_EV_20241115_F1_seed3_20261029T0651UTC.json` SHA `c506bc4a442f8fbbd861fcc54e37ddd2f1b77f133e7a675a9d52bfaad49baac0`：7/7方法齐备，seed、scenario fingerprint、输入hash一致，EV2 profile均为冻结SHA；各法逐轮validator通过、硬约束和一对一错误为0。各法服务12/572单、平均接驾103.017秒、模拟EV2能耗4.901030 kWh，单格指标相同。去重调用8次（D5=7、D8=1、D6/D7额外调用=0），8/8 trace有完成usage，合计120,721 tokens、额度估值0.350110 credits/API等值$0.0140044，真实账户扣费未知。它只是一格legacy描述证据，不并入EV2 v4 seed0或总体排名。
- v5离线源覆盖索引仍是313项；相对T0654的324项，有11个后续结果未在该快照内。未改写源结果，也未为覆盖差重算sidecar、重发模型请求或重跑SUMO；路线选择和利润仍blocked/null。


## 2026-10-29 15:12 CST 增量：EV2 seed0新增完整格，legacy seed3单格复核完成

证据类型：EV2官方segment-ledger与冻结输入指纹审计、EV2完整格逐轮和Codex trace复核；隔离legacy seed3完整格审计。当前运行状态仅作快照。

- EV2 seed0官方账本 `audit_snapshots/ev2_segment_ledger_audit_20261029T0705UTC_live.json` SHA-256 `dab5df047c2dad25acde38eaa49f7d4b64a5ea5c75ebc84eac00590885b1df0c`：326/630有效、304缺失、0无效；D0/D1/D2各90，D5=13，D6/D7=14，D8=15。90个场景—车队格中13格有七方法输出，整体矩阵仍不完整。匹配指纹快照 `ev2_input_fingerprint_audit_20261029T070544Z_monitor.json` SHA `8f64ed41696eb7a280df1474040a98e9b5598e5f9d6e3a15e5827239938e8ca8`核验326项，scenario/input mismatch与逐项错误均为0，profile SHA统一为`b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。
- 新完整格 `EV_20240411_200000_q50/F2` 的单元审计SHA `0c4136d2c48ac4ebbe0f1626673beadb34b9e3b69c999dc46d3567cf31a746fa`、唯一调用审计SHA `c8626989bab81ec28d40d6ec61b9e2ae52bfe868a4910057b7ff5e1b3a0182b8`：7/7方法完整，共享scenario fingerprint `948e786096ea741187525220f04c05b1889bc988b2e8686248bb7ac1f5d8a264`和冻结输入；全部544轮（D8为545轮）逐轮validator有效，硬约束错误与一对一错误均为0。D5服务24/540单，平均接驾93.819秒、P95 246.398秒、记录EV2总billable能耗17.599535 kWh；D5有15个成功唯一调用。D8有1个调用、13,734 tokens；D6/D7重放同一D8响应、无新增调用。该格唯一计16次调用、294,736 tokens（292,465 input，含190,080 cached；2,271 output），标准额度估值0.675095 credits、API等值$0.0270038，真实账户扣费未报告。16/16 trace有完成usage，失败、未知投递、429、retry-limit和重复thread均为0。仅作该格描述性证据，不外推总体排名。
- `EV_20240411_200000_q50/F4/D8`新结果SHA `50c917b44e4ea2962295cdd0f55b1883e1bac7ab39a7ba1f11267c27a1306586`通过账本与指纹审计：565轮、逐轮validator错误0、D8策略被接受，服务49/540单，平均接驾97.030秒、P95 273.504秒、记录EV2 billable能耗30.848517 kWh。唯一调用13,610 tokens，估值0.071975 credits/API等值$0.002879，真实账户扣费未知。该格在T0705快照仍缺D5/D6/D7，不能作完整七方法对照；D6/D7策略复放正在进行。
- 隔离legacy `dispatch-agent-v1` seed3新完整格 `EV_20241115_224000_q75/F2` 审计SHA `054158ab282af0d228312de091759755a53dc27432c2aed7ac49824319d75aad`、唯一调用审计SHA `7a31ba1eb7ca89a319c0f3bf606ca973b65aac6e608aa54f6f6f34bcb293ae7d`：7/7方法完整，同一输入与scenario fingerprint `7e8130c6eeaafd8e2a62453a9941ab68870aee53bca1179fa71dac284f1b0dc6`，profile SHA匹配冻结EV2 profile，逐轮validator通过。该格D5服务24/572单、平均接驾123.745秒、P95 273.504秒，输出记录的模拟EV2总能耗9.589869 kWh；全格15次唯一调用（D5=14、D8=1；D6/D7复用D8），251,567 tokens（248,743 input，含152,064 cached；2,824 output），标准额度估值0.644147 credits/API等值$0.02576588，真实账户扣费未知。trace没有失败、未知投递或rate-limit错误。此legacy格与EV2 v4 seed0分开，不合并进326项或13个完整格。
- v5离线time-money侧账最近快照为T1410单run/50段（SHA `027e28d17305f63f50e46b726628bab2a57b679c0804edb63a39f752758a6d1c`）；T1405合计8 runs/360段（SHA `6e41f9e75cec3e5a676443789252b5f12eda40134ca8a40174056711f3f73d4d`）。已纳入源结果按每段`max(signed_net_kwh,0)`计量，乘客收入按段一次、空驶收入0、缺失票价留null；源结果未改、错误列表为空、没有排序、模型调用或SUMO重跑。侧账coverage index仍为313个结果（manifest SHA `7a671920df430eb00812bf93ba83f2fb230092c64433ad81250179c4015d189a`），早于T0705的326项账本；费率、完整共同候选池仍缺，route-choice/profit保持blocked/null。
- T0705矩阵D5累计201条CLI trace中，189条turn.completed、12条turn.failed且带HTTP429/retry-limit记录；usage coverage不完整。失败保留failed，不重试。本轮新增EV2 F2 D5和seed3 F2 D5 trace均正常完成，不增加失败数。至07:12 UTC，EV2 seed0 F4/D6-D7确定性复放与legacy seed3 F4/D0-D1基线都仍在运行；两者均无新模型调用。
- NYC输入仍是TLC真实订单需求映射到zone-level OD成本代理，pickup使用合成质心代理，不是街道级路网或实车遥测。D0不以能耗优化，D5不使用确定性全局能耗目标；总体结果尚不支持方法排名。


## 2026-10-29 15:36 CST 增量：NYC EV2新增完整格，seed0与隔离legacy seed3继续运行

证据类型：NYC EV2正式segment-ledger和输入指纹审计；已完成D5单元逐轮校验与Codex trace；time-money v5离线侧车增量审计；隔离legacy seed3运行状态快照。

- **EV2正式seed0**：最新segment-ledger `audit_snapshots/ev2_segment_ledger_audit_20261029T0733UTC_live.json` SHA-256 `5370b506d90fd634008cc14b33911297c7b87bcf5cc15820ce882f36566e97ec`：332/630项有效、298缺失、0无效；D0/D1/D2各90，D5为14，D6/D7/D8各16。七方法完整格14/90，整体矩阵仍不完整。匹配指纹审计 `ev2_input_fingerprint_audit_20261029T0733UTC_live.json` SHA `66019e1b86b7ab9335097368107581d48204893be295115a8bc71f30e105dc30`：332项场景指纹、冻结输入和契约均匹配，mismatch/逐项错误均0；EV2 profile SHA统一为 `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。
- 新完成D5单元 `EV_20240411_200000_q50/F4`结果SHA `accba0298ee82118de30e165e21a16b7ab5ca4214e7916511e0ca180a2de959a`：563轮、`partial_run=false`，563/563轮validator通过、硬约束错误0、一对一错误0；49/540单完成，平均接驾92.077秒，P95 273.504秒，模拟EV2能耗30.725348 kWh。该格由此成为第14个七方法齐备格。D5 30条唯一Codex调用trace均turn.completed，0失败/未知/429/重试，728,226 input（380,160 cached）+4,667 output=732,893 tokens；28条策略输出被接受，2条返回不可行候选并被拒，未重试。D5全矩阵12条历史HTTP 429仍保持failed。D5累计231调用/219成功/12失败、0未知，4,868,505可用tokens；因12条失败无usage，usage coverage仍不完整。
- **time-money v5离线侧车**：截至332个有效源结果的去重覆盖索引 `time_money_v5_source_coverage_index_20261029T0735UTC_332/manifest.json` SHA `13629c6bfbc4d95a252f9f15dfcc56194cee862905ad75758b603faa2062090c`。本轮增量为3个源结果、78条行程分段；审计 `audit_snapshots/time_money_v5_delta_audit_20261029T0735UTC.json` SHA `4ee56e7111e78d3b62c120829db327ede956bc0d78b2c272795f0c37df03fe6f`：逐段 `max(signed_net_kwh,0)`差异0、重复乘客收入0、空驶非零收入0，源哈希复核通过且结果未修改；缺冻结电价/完整共同候选池，路线选择和利润仍blocked/null，0排序、0新模型调用、0 SUMO重跑。侧车不是在线selector。
- **EV2运行中状态**：D8在 `EV_20240427_223500_q75/F1` 完成后，D6/D7复用该D8策略的确定性回放于07:33:49Z完成；三项结果每项527轮均通过validator、13/527订单服务，回放不增加模型调用。随后seed0进入该格D5 live；截至07:35Z已有8条trace全部turn.completed、0失败/未知，usage为153,641 input（88,704 cached）+1,039 output，仿真仍运行。seed0进度JSONL显示parent PID 71952及当前唯一D5子进程PID 13536。
- **隔离legacy seed3**：`EV_20241115_224000_q75/F4` 的D0/D1/D2均已完整结束，554轮逐轮validator均通过，0硬约束/一对一错误，每法49/572单；该批继续单独于EV2统计。D8请求trace已完成1次（13,451 input+216 output tokens，无失败/未知），但仿真尚未落最终结果；截至07:35Z子进程PID 11176仍运行。
- **状态记录差异**：EV2 `experiment_manifest.json` SHA `de59b38a80b03ea6320b870cb34dc89d9bfcc7e4c9229ee0991731e2cdb50a6f`最后更新于04:19Z，虽status为running，`phases.evaluation`仍标`interrupted_for_shared_cli_contention`；其后progress JSONL和唯一活跃parent/child显示评估已经继续。未改manifest/官方seed文件，未启动第二个seed0 owner。
- 研究边界不变：TLC真实订单只映射zone-level OD成本代理，接驾为质心代理，不是街道级路网/真实ETA；D0无能耗目标，D5无确定性全局能耗优化，D1/D2/D6/D7/D8仍按冻结词典序把EV2 signed-net能耗置于末级。


## 2026-10-29 15:40 CST 增量：EV2 seed0第二个新增七法完整格

证据类型：seed0官方segment-ledger和指纹审计；F1/D5完整结果与逐轮validator、Codex trace；对应time-money v5离线侧车；两条仍在运行的NYC批次进度记录。

- 最新seed0正式账本 `audit_snapshots/ev2_segment_ledger_audit_20261029T0738UTC_live.json` SHA `c4c4e402347e947f2932a148ea5c676db31383d238dbe24bd036277643407ec2`：333/630有效、297缺失、0无效；D0/D1/D2各90，D5为15，D6/D7/D8各16；七方法齐备格15/90。指纹审计SHA `74078a33667ba3eff5a825e65c54e11d0d301c3b452e0f223d5a7e7f68757e6e`覆盖333项，冻结输入、场景/同格指纹、契约和profile匹配，错误0；profile SHA `b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee`。
- 新完成单元 `EV_20240427_223500_q75/F1/D5` SHA `dc1631311df7072b72e9021cbafed5fa800559125f0dd0bcf3209f2669cb1612`：527轮、`partial_run=false`、527/527 validator valid、硬约束和一对一错误0；13/573单完成，平均接驾145.182秒。8次Codex CLI调用全部turn.completed且策略输出8/8接受，0失败/未知/重试，154,680 tokens（153,641 input，88,704 cached，1,039 output），估算0.400207 credits，账户实际扣费未知。该格补齐第15个完整七法cell；单格仍不用于整体排序或性能结论。
- 侧车以333个有效源结果建立覆盖索引，manifest SHA `ac9b04e001a50e1439993aab9bce7ad4d5d16ab4e6396ac68c45f1a2c90681e0`。本次只给新D5结果追加26个EV2分段（13空驶、13载客）；增量审计SHA `e2670ea824c7681181c28e6a2736cdb55995542b878bffc3188cfdcde7382d0a`确认signed-net截断公式0差异、重复收入0、源结果未改；电价/共同路线池仍不完整，成本和路线选择阻断、利润null、无排名，0模型请求/0 SUMO。
- **在途**：seed0进度已从F1 D5推进到 `EV_20240427_223500_q75/F2/D8_live`（07:36:42Z开始）。截至07:38:42Z，该格已产生1条完成trace（13,451 input含12,672 cached +191 output），仿真尚未完成；不计入333项账本。隔离legacy seed3的 `EV_20241115_224000_q75/F4/D8`已完成：557轮validator全valid、1次响应接受、49/572单、13,667 tokens；D6/D7确定性回放于07:37:06Z后继续，尚未完成，仍独立统计。
- D5全矩阵的12个历史HTTP429失败保持不重试；本次D5 F1单元无429。NYC仍是TLC真实订单映射到zone-level OD代理、合成质心接驾代理，不代表街道级路网或实时ETA。
