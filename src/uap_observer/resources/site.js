(() => {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const archiveThemeFor = (pathname) => {
    if (pathname.includes("/news/")) return "daily";
    if (pathname.includes("/events/")) return "events";
    if (pathname.endsWith("/timeline.html")) return "timeline";
    if (pathname.includes("/relationships") || pathname.includes("/tags")) return "evidence";
    return "home";
  };

  if (!reducedMotion) {
    const applyArchiveTheme = () => {
      document.body.dataset.archiveTheme = archiveThemeFor(window.location.pathname);
    };
    const transition = document.createElement("div");
    transition.className = "page-transition";
    transition.setAttribute("aria-hidden", "true");
    document.body.appendChild(transition);
    applyArchiveTheme();

    requestAnimationFrame(() => {
      requestAnimationFrame(() => transition.classList.add("is-ready"));
    });

    document.addEventListener("click", (event) => {
      const link = event.target.closest("a[href]");
      if (!link || event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if ((link.target && link.target !== "_self") || link.hasAttribute("download")) return;

      const destination = new URL(link.href, window.location.href);
      if (destination.origin !== window.location.origin) return;
      if (destination.pathname === window.location.pathname && destination.hash) return;

      event.preventDefault();
      transition.dataset.destination = archiveThemeFor(destination.pathname);
      transition.classList.remove("is-ready");
      transition.classList.add("is-leaving");
      window.setTimeout(() => {
        window.location.href = destination.href;
      }, 720);
    });

    window.addEventListener("pageshow", () => {
      applyArchiveTheme();
      transition.classList.remove("is-leaving");
      transition.classList.add("is-ready");
    });
  }

  const sections = document.querySelectorAll("[data-reveal]");
  if (!sections.length) return;

  if (!("IntersectionObserver" in window)) {
    sections.forEach((section) => section.classList.add("is-visible"));
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    },
    { threshold: 0.14 },
  );

  sections.forEach((section) => observer.observe(section));
})();
