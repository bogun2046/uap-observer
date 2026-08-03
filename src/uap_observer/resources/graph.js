(() => {
  const root = document.querySelector("[data-person-graph]");
  if (!root) return;

  const canvas = root.querySelector("[data-graph-canvas]");
  const detail = root.querySelector("[data-graph-detail]");
  const tagSelect = root.querySelector("[data-graph-tag]");
  const kindSelect = root.querySelector("[data-graph-kind]");
  const resetButton = root.querySelector("[data-graph-reset]");
  const meta = root.querySelector("[data-graph-meta]");
  const relationLabels = {
    supports: "支持",
    questions: "提问",
    criticizes: "质疑",
    responds_to: "回应",
    quotes: "引述",
    works_with: "合作",
    investigates: "调查",
    participates_with: "共同参与",
    affiliated_with: "关联",
  };
  const statusLabels = {
    candidate: "待核验",
    corroborated: "多来源支持",
    verified: "已核验",
    disputed: "存在争议",
  };

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

  const showError = (message) => {
    canvas.innerHTML = `<p class="graph-empty">${escapeHtml(message)}</p>`;
  };

  const evidenceLinks = (ids) => {
    if (!Array.isArray(ids) || !ids.length) return "暂无已关联的证据新闻。";
    return ids.map((id) => `<a href="news/${encodeURIComponent(id)}.html">新闻 #${escapeHtml(id)}</a>`).join("、");
  };

  fetch("graph.json", { headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) throw new Error(`图谱数据加载失败（${response.status}）`);
      return response.json();
    })
    .then((payload) => {
      const tags = Array.isArray(payload.tags) ? payload.tags : [];
      tags.forEach((tag) => {
        const option = document.createElement("option");
        option.value = tag.slug;
        option.textContent = `${tag.name}（${tag.person_ids.length}）`;
        tagSelect.appendChild(option);
      });
      const tagMembers = new Map(tags.map((tag) => [tag.slug, new Set(tag.person_ids)]));
      const requestedTag = new URLSearchParams(window.location.search).get("tag");
      if (requestedTag && tagMembers.has(requestedTag)) tagSelect.value = requestedTag;
      const elements = [
        ...(Array.isArray(payload.nodes) ? payload.nodes : []),
        ...(Array.isArray(payload.edges) ? payload.edges : []),
      ];

      if (!Array.isArray(payload.nodes) || payload.nodes.length === 0) {
        showError("暂无已建立的人物实体或关系数据。");
        return;
      }

      if (!window.cytoscape) throw new Error("关系图组件尚未加载");
      const cy = window.cytoscape({
        container: canvas,
        elements,
        minZoom: 0.2,
        maxZoom: 2.4,
        wheelSensitivity: 0.18,
        style: [
          { selector: "node", style: {
            "background-color": "#79c5e3",
            "border-color": "#b3e7f6",
            "border-width": 1,
            "color": "#f0f6fa",
            "font-size": 11,
            "label": "data(name)",
            "text-wrap": "ellipsis",
            "text-max-width": 100,
            "text-valign": "bottom",
            "text-margin-y": 7,
            "width": 18,
            "height": 18,
          } },
          { selector: "edge", style: {
            "line-color": "#6d899d",
            "target-arrow-color": "#6d899d",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            "width": "mapData(evidence_count, 1, 8, 1, 5)",
            "opacity": 0.72,
          } },
          { selector: 'edge[kind = "cooccurrence"]', style: {
            "line-style": "dashed",
            "line-color": "#8ea2b4",
            "target-arrow-shape": "none",
            "opacity": 0.46,
          } },
          { selector: 'edge[kind = "explicit"]', style: {
            "line-color": "#79c5e3",
            "target-arrow-color": "#79c5e3",
          } },
          { selector: ".is-hidden", style: { display: "none" } },
          { selector: ".is-selected", style: {
            "border-color": "#f0f6fa",
            "border-width": 3,
            "line-color": "#f0f6fa",
            "target-arrow-color": "#f0f6fa",
          } },
        ],
        layout: { name: "cose", animate: false, padding: 36, idealEdgeLength: 130 },
      });

      const updateMeta = () => {
        const visibleNodes = cy.nodes().filter((node) => !node.hasClass("is-hidden")).length;
        const visibleEdges = cy.edges().filter((edge) => !edge.hasClass("is-hidden")).length;
        meta.textContent = `当前视图包含 ${visibleNodes} 位人物和 ${visibleEdges} 条关系。实线为明确关系，虚线为共同出现。`;
      };

      const applyFilters = () => {
        const selectedTag = tagSelect.value;
        const selectedKind = kindSelect.value;
        const members = tagMembers.get(selectedTag);
        cy.nodes().forEach((node) => {
          const entityId = Number(node.data("entity_id"));
          node.toggleClass("is-hidden", Boolean(members && !members.has(entityId)));
        });
        cy.edges().forEach((edge) => {
          const hiddenByKind = selectedKind && edge.data("kind") !== selectedKind;
          const hiddenByNode = edge.source().hasClass("is-hidden") || edge.target().hasClass("is-hidden");
          edge.toggleClass("is-hidden", Boolean(hiddenByKind || hiddenByNode));
        });
        updateMeta();
      };

      const showNode = (node) => {
        const data = node.data();
        detail.innerHTML = `<strong>${escapeHtml(data.name)}</strong>`
          + (data.organization ? `<p>机构：${escapeHtml(data.organization)}</p>` : "")
          + (data.country ? `<p>国家/地区：${escapeHtml(data.country)}</p>` : "")
          + `<p>点击相邻节点继续探索。</p>`;
      };

      const showEdge = (edge) => {
        const data = edge.data();
        const label = data.kind === "cooccurrence" ? "共同出现" : (relationLabels[data.label] || data.label);
        const quotes = Array.isArray(data.evidence_quotes) && data.evidence_quotes.length
          ? `<p>摘录：${data.evidence_quotes.map((quote) => `“${escapeHtml(quote)}”`).join("；")}</p>`
          : "";
        detail.innerHTML = `<strong>${escapeHtml(edge.source().data("name"))} → ${escapeHtml(edge.target().data("name"))}</strong>`
          + `<p>关系：${escapeHtml(label)}；状态：${escapeHtml(statusLabels[data.status] || data.status)}</p>`
          + `<p>证据数量：${escapeHtml(data.evidence_count || 0)}；证据：${evidenceLinks(data.evidence_news_ids)}</p>`
          + quotes;
      };

      cy.on("tap", "node", (event) => {
        cy.elements().removeClass("is-selected");
        event.target.addClass("is-selected");
        showNode(event.target);
      });
      cy.on("tap", "edge", (event) => {
        cy.elements().removeClass("is-selected");
        event.target.addClass("is-selected");
        showEdge(event.target);
      });
      tagSelect.addEventListener("change", applyFilters);
      kindSelect.addEventListener("change", applyFilters);
      resetButton.addEventListener("click", () => {
        tagSelect.value = "";
        kindSelect.value = "";
        cy.elements().removeClass("is-hidden is-selected");
        cy.fit(undefined, 36);
        detail.textContent = "选择一个人物或关系查看证据。";
        updateMeta();
      });
      applyFilters();
    })
    .catch((error) => showError(error.message || "图谱数据暂时不可用。"));
})();
