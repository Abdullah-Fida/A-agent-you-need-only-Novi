import Link from 'next/link';
import Image from 'next/image';
import { Article } from '@/lib/supabase';

interface ArticleCardProps {
  article: Article;
  showImage?: boolean;
}

export default function ArticleCard({ article, showImage = true }: ArticleCardProps) {
  const date = new Date(article.published_at).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
  });

  return (
    <Link href={`/${article.slug}`} className="card">
      {showImage && article.main_image_url && (
        // next/image resizes, converts to AVIF/WebP and serves from the CDN,
        // rather than hot-linking the publisher's full-size original.
        <Image
          className="card-img"
          src={article.main_image_url}
          alt=""
          width={600}
          height={400}
          sizes="(max-width: 700px) 100vw, 33vw"
          loading="lazy"
        />
      )}
      <span className="tag">{article.category || 'News'}</span>
      {/* h3, not h2: the section name above this card is the h2, and a card
          title at the same level flattens the outline that both screen
          readers and search engines read the page structure from. */}
      <h3>{article.title}</h3>
      {article.summary && <p>{article.summary}</p>}
      <div className="byline">
        <time dateTime={article.published_at}>{date}</time>
        {article.reading_minutes ? (
          <>
            <span aria-hidden="true">·</span>
            <span>{article.reading_minutes} min read</span>
          </>
        ) : null}
      </div>
    </Link>
  );
}
