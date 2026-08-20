"use strict";

(function exposeDraftState(root) {
  function generateButtonState({ status, busy, blocked }) {
    if (status !== "collecting") {
      return { disabled: true, label: "确认资料并开始生成" };
    }
    if (busy) {
      return { disabled: true, label: "正在冻结资料清单" };
    }
    if (blocked) {
      return { disabled: true, label: "请先移除不可用资料" };
    }
    return { disabled: false, label: "确认资料并开始生成" };
  }

  const api = Object.freeze({ generateButtonState });
  root.LarkflowDraftState = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis === "undefined" ? window : globalThis);
