// Reveal-on-scroll for cards and sections
const observer = new IntersectionObserver(
  (entries) => {
    entries.forEach((e) => {
      if (e.isIntersecting) {
        e.target.classList.add("in");
        observer.unobserve(e.target);
      }
    });
  },
  { threshold: 0.12 }
);

document.querySelectorAll(".card, .section-head, .tl-item, .stat").forEach((el) => {
  el.classList.add("reveal");
  observer.observe(el);
});

// Smooth active nav highlight
const sections = document.querySelectorAll("section[id]");
const navLinks = document.querySelectorAll(".nav-links a");
window.addEventListener("scroll", () => {
  let current = "";
  sections.forEach((s) => {
    if (window.scrollY >= s.offsetTop - 120) current = s.id;
  });
  navLinks.forEach((a) => {
    a.style.color = a.getAttribute("href") === "#" + current ? "var(--text)" : "";
  });
});

// Console easter egg
console.log("%cDiLoCo WAN", "color:#22d3ee;font-weight:800;font-size:18px;");
console.log("Distributed Low-Communication Training over the Internet");