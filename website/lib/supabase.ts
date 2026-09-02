import { createClient, SupabaseClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || '';
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || '';

export const isConfigured = Boolean(supabaseUrl && supabaseAnonKey);

if (!isConfigured) {
  console.warn(
    '[novi] NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY are not set. ' +
    'The site will build and render, but no articles will load.'
  );
}

/*
 * createClient() THROWS ("supabaseUrl is required") when either value is
 * empty, and this module is imported by every page — so a missing env var
 * failed the entire Vercel build rather than just showing an empty site.
 *
 * We only construct the client when both values exist; every query below
 * checks `isConfigured` first and returns empty results otherwise.
 */
export const supabase: SupabaseClient | null = isConfigured
  ? createClient(supabaseUrl, supabaseAnonKey)
  : null;

/** Mirrors the `articles` table in database/schema.sql. */
export interface Article {
  id: string;
  title: string;
  slug: string;
  content: string;
  summary: string;
  main_image_url: string;
  category: string;
  seo_keywords: string[];
  meta_title?: string;
  meta_description?: string;
  reading_minutes?: number;
  word_count?: number;
  source_url?: string;
  source_name?: string;
  author: string;
  status?: string;
  views?: number;
  published_at: string;
  created_at?: string;
}

/** Latest published articles, newest first. */
export async function getArticles(limit = 24, category?: string): Promise<Article[]> {
  if (!supabase) return [];
  try {
    let query = supabase
      .from('articles')
      .select('*')
      .eq('status', 'published')
      .order('published_at', { ascending: false })
      .limit(limit);

    if (category) query = query.eq('category', category);

    const { data, error } = await query;
    if (error) {
      console.error('[novi] Failed to load articles:', error.message);
      return [];
    }
    return (data as Article[]) || [];
  } catch (err) {
    console.error('[novi] Unexpected error loading articles:', err);
    return [];
  }
}

/** Distinct categories that actually have published articles. */
export async function getCategories(): Promise<string[]> {
  if (!supabase) return [];
  try {
    const { data } = await supabase
      .from('articles')
      .select('category')
      .eq('status', 'published');
    const set = new Set((data || []).map((r: { category: string }) => r.category).filter(Boolean));
    return Array.from(set).sort();
  } catch {
    return [];
  }
}

/**
 * Everything a given byline has published, newest first.
 *
 * Takes a LIST of names because the archive carries more than one: articles
 * published before the named byline existed say "PressVane Newsroom", and
 * two spellings of that were live at different times. Without them the
 * author page would show a fraction of the work it is meant to evidence.
 */
export async function getArticlesByAuthor(
  names: string[],
  // High enough to hold the whole archive. At 60 the page said "60
  // articles" when 84 matched, which understates the body of work the page
  // exists to evidence -- and every card is also an internal link, so a
  // truncated list costs crawl paths as well as credibility.
  limit = 500,
): Promise<Article[]> {
  if (!supabase || names.length === 0) return [];
  try {
    const { data, error } = await supabase
      .from('articles')
      .select('*')
      .eq('status', 'published')
      .in('author', names)
      .order('published_at', { ascending: false })
      .limit(limit);

    if (error) {
      console.error('[pressvane] Failed to load author articles:', error.message);
      return [];
    }
    return (data as Article[]) || [];
  } catch (err) {
    console.error('[pressvane] Unexpected error loading author articles:', err);
    return [];
  }
}
