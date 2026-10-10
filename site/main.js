// aeradex landing page: progressive enhancement, no tracking. The demo video plays muted only while visible.
(() => {
  'use strict';
  const root = document.documentElement;
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Theme: dark is the default, the choice is remembered per browser.
  const themeButton = document.querySelector('#themeBtn');
  const isDark = () => root.dataset.theme !== 'light';
  const updateThemeLabel = () => {
    const label = isDark() ? 'Helle Darstellung aktivieren' : 'Dunkle Darstellung aktivieren';
    themeButton.setAttribute('aria-label', label);
    themeButton.title = label;
    document.querySelector('meta[name="theme-color"]').content = isDark() ? '#08090a' : '#f7f8f6';
  };
  themeButton.addEventListener('click', () => {
    root.dataset.theme = isDark() ? 'light' : 'dark';
    try { localStorage.setItem('aeradex-theme', root.dataset.theme); } catch (_) { /* Storage may be unavailable. */ }
    updateThemeLabel();
  });
  updateThemeLabel();

  const nav = document.querySelector('#nav');
  const updateNav = () => nav.classList.toggle('scrolled', scrollY > 8);
  addEventListener('scroll', updateNav, { passive: true });
  updateNav();

  // Product tabs.
  const tabs = [...document.querySelectorAll('.ui-tabs [role="tab"]')];
  const panels = [...document.querySelectorAll('.product-panel')];
  const caption = document.querySelector('#uiCap');
  const select = (index, focus = false) => {
    tabs.forEach((tab, i) => {
      const active = i === index;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      panels[i].hidden = !active;
    });
    // Captions are authored locally in index.html, never supplied externally.
    caption.innerHTML = tabs[index].dataset.cap;
    if (focus) tabs[index].focus();
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => select(index));
    tab.addEventListener('keydown', (event) => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); select(next, true); }
    });
  });
  if (tabs.length) select(0);

  // Reveal on scroll.
  const revealed = document.querySelectorAll('.reveal');
  if (reduceMotion || !('IntersectionObserver' in window)) {
    revealed.forEach(el => el.classList.add('in'));
  } else {
    const io = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('in');
        io.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    revealed.forEach(el => io.observe(el));
  }

  // Agent sequence in the hero: lines appear one after another, once.
  const log = document.querySelector('#agentLog');
  if (log && !reduceMotion) {
    const lines = [...log.children];
    const prompt = lines[0];
    const text = prompt.lastChild.textContent;
    log.classList.add('play');
    const typePrompt = (done) => {
      prompt.classList.add('on', 'caret');
      prompt.lastChild.textContent = '';
      let i = 0;
      const tick = () => {
        prompt.lastChild.textContent = text.slice(0, ++i);
        if (i < text.length) setTimeout(tick, 28 + Math.random() * 30);
        else { prompt.classList.remove('caret'); setTimeout(done, 380); }
      };
      tick();
    };
    const showRest = (i = 1) => {
      if (i >= lines.length) return;
      lines[i].classList.add('on');
      setTimeout(() => showRest(i + 1), i === lines.length - 2 ? 700 : 420);
    };
    setTimeout(() => typePrompt(() => showRest()), 700);
  }

  // Call demo: muted while in view (subtitles are burned in), sound on request.
  const video = document.querySelector('#callVideo');
  if (video) {
    if (matchMedia('(max-width: 640px) and (orientation: portrait)').matches) video.poster = 'img/larissa-poster-9x16.webp';
    const sound = document.querySelector('#callSound');
    sound.hidden = false;
    const syncSound = () => { sound.hidden = !video.muted; };
    sound.addEventListener('click', () => {
      video.muted = false;
      video.currentTime = 0;
      video.play().catch(() => {});
    });
    video.addEventListener('volumechange', syncSound);
    if (!reduceMotion && 'IntersectionObserver' in window) {
      new IntersectionObserver(([entry]) => {
        if (entry.isIntersecting) { if (video.muted) video.play().catch(() => {}); }
        else video.pause();
      }, { threshold: 0.5 }).observe(video);
    }
  }

  // Copy the terminal commands.
  document.querySelectorAll('[data-copy]').forEach(button => {
    button.addEventListener('click', async () => {
      const source = document.getElementById(button.dataset.copy);
      const commands = source.textContent.split('\n').map(line => line.replace(/^\$ /, '')).join('\n');
      try {
        await navigator.clipboard.writeText(commands);
        button.textContent = 'Kopiert';
      } catch (_) {
        button.textContent = 'Nicht möglich';
      }
      setTimeout(() => { button.textContent = 'Kopieren'; }, 1600);
    });
  });

  // Spotlight that follows the pointer on feature cards.
  if (!reduceMotion) {
    document.querySelectorAll('.card').forEach(card => {
      card.addEventListener('pointermove', (event) => {
        const box = card.getBoundingClientRect();
        card.style.setProperty('--mx', `${event.clientX - box.left}px`);
        card.style.setProperty('--my', `${event.clientY - box.top}px`);
      });
    });
  }
})();
