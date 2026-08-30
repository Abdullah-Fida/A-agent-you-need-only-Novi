'use client';

import { useEffect, useState } from 'react';

type Theme = 'light' | 'dark';

/**
 * Light/dark switch.
 *
 * The page follows the reader's system setting by default, which means anyone
 * whose machine is in dark mode never sees the light treatment — and a news
 * site is read in daylight as often as not. This lets them choose, and
 * remembers the choice.
 *
 * The chosen theme is written to data-theme on <html>, which globals.css
 * already honours in both directions. The initial value is applied by an
 * inline script in the layout, before first paint, so the page never flashes
 * the wrong theme.
 */
export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    const stamped = document.documentElement.getAttribute('data-theme');
    if (stamped === 'light' || stamped === 'dark') {
      setTheme(stamped);
      return;
    }
    setTheme(
      window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
    );
  }, []);

  function toggle() {
    const next: Theme = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    document.documentElement.setAttribute('data-theme', next);
    try {
      localStorage.setItem('pressvane-theme', next);
    } catch {
      /* private browsing — the choice simply won't persist */
    }
  }

  // Render the button before hydration too, so the masthead doesn't reflow.
  const isDark = theme === 'dark';

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={toggle}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={isDark ? 'Light' : 'Dark'}
    >
      {isDark ? (
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="4.2" />
          <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
        </svg>
      ) : (
        <svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M20.7 14.6A8.5 8.5 0 0 1 9.4 3.3a1 1 0 0 0-1.3-1.2 10.5 10.5 0 1 0 13.8 13.8 1 1 0 0 0-1.2-1.3z" />
        </svg>
      )}
    </button>
  );
}
