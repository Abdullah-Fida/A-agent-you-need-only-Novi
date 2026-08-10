import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  images: {
    // Article images come from whichever outlet published the original story,
    // plus whatever the image generator produces, so the host list cannot be
    // enumerated ahead of time. Allow any HTTPS source but keep the optimizer
    // in front of it, so everything is resized, converted to AVIF/WebP and
    // served from the CDN rather than hot-linked at full size.
    remotePatterns: [{ protocol: "https", hostname: "**" }],
    formats: ["image/avif", "image/webp"],
    minimumCacheTTL: 86400,
  },

  // Strips the "X-Powered-By: Next.js" header
  poweredByHeader: false,

  // Trailing-slash inconsistency creates duplicate URLs for the same page,
  // which splits ranking signals between them.
  trailingSlash: false,

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "SAMEORIGIN" },
        ],
      },
      {
        // Brand assets are immutable; let the CDN keep them.
        source: "/:file(logo.png|og-default.png|icon-192.png|icon-512.png|apple-icon.png)",
        headers: [
          { key: "Cache-Control", value: "public, max-age=31536000, immutable" },
        ],
      },
    ];
  },
};

export default nextConfig;
