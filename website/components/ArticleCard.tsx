import Link from 'next/link';
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
        /* eslint-disable-next-line @next/next/no-img-element */
        <img
          className="card-img"
          src={article.main_image_url}
          alt=""
          loading="lazy"
        />
      )}
      <span className="tag">{article.category || 'News'}</span>
      <h2>{article.title}</h2>
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
