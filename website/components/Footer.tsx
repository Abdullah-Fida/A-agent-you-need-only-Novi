export default function Footer() {
  return (
    <footer className="site-footer">
      <div className="main-container">
        <div className="footer-content">
          <div className="footer-brand">
            <h3 style={{ fontFamily: 'var(--font-heading)', fontSize: '1.2rem', marginBottom: '8px' }}>
              DAILY PULSE <span className="gradient-text">PK</span>
            </h3>
            <p>
              Autonomous real-time news breakdown powered by AI subagents. Breaking stories, market analysis, and technology insights.
            </p>
          </div>
          <div style={{ color: 'var(--text-dim)', fontSize: '0.85rem', alignSelf: 'flex-end' }}>
            © {new Date().getFullYear()} Daily Pulse PK. All rights reserved. Powered by NOVI Engine.
          </div>
        </div>
      </div>
    </footer>
  );
}
