# 图示验证记录

两张图均使用本地 Archify 生成。HTML 经确定性检查，SVG 使用该 HTML 的导出功能生成；PNG 由 Chromium 渲染 SVG，供不支持 SVG 的阅读器使用。

Disruptor 图依据 `hft/disruptor_cpp20/examples/diamond.cpp` 的 A/B/C 依赖关系，以及 `include/disruptor.hpp` 的 `start()`、`capacity_for()`、`available_batch()` 和 `run()`。SPSC 图解释文章教学队列的两条 acquire/release 同步链。

## disruptor-diamond

- diagram_type: `architecture`
- output: `disruptor-diamond.html`
- specification_sha256: `5c77869dfd5b0c95434a155d8236c5afa14a59b51cdba6517a0e9def83086b84`
- artifact_sha256: `4328b746159e977cc45d84164f90e016277e43b75033575fe62fe5132ccef108`
- validation: 9/9 showcase, 0 errors, 0 warnings
- browser_evidence: passed
- visual_review: passed
- correction_rounds: 2

浏览器检查覆盖 1440×900、1600×1000、1920×1080、2048×1320；四种尺寸均无横向或纵向溢出。已查看浅色、深色截图，并检查最终导出的 SVG/PNG；节点、连线、标签和同步方向未见遮挡或歧义。

对应回执：`disruptor-diamond.delivery.json`、`disruptor-diamond.visual-check.json`、`disruptor-diamond.export.json`。

## spsc-memory-order

- diagram_type: `sequence`
- output: `spsc-memory-order.html`
- specification_sha256: `fdd6dd8e0886aa00388ddecc0cef59432bc239ca5fb0173723065f3e0866a49e`
- artifact_sha256: `986bba9dfbd0ce245b0ecde8ae998ee15f72e83a147045ab6d1c19a9a45d1f50`
- validation: 9/9 showcase, 0 errors, 0 warnings
- browser_evidence: passed
- visual_review: passed
- correction_rounds: 1

浏览器检查覆盖 1440×900、1600×1000、1920×1080、2048×1320；四种尺寸均无横向或纵向溢出。已查看浅色、深色截图，并检查最终导出的 SVG/PNG；节点、连线、标签和同步方向未见遮挡或歧义。

对应回执：`spsc-memory-order.delivery.json`、`spsc-memory-order.visual-check.json`、`spsc-memory-order.export.json`。
