package channel

import (
	"sort"
	"strings"
	"time"

	"eagle-eye/xhs/internal/xhs"
)

// 发布时间来源（extra.created_at_source）。
const (
	TimeFromDetail = "detail"  // 打开笔记读到的 note.time
	TimeFromNoteID = "note_id" // 由笔记 ID（ObjectId）前 4 字节推算，与 note.time 精确一致
)

// count 计数字符串转 *int；空串表示页面没给（未知）→ nil。
func count(s string) *int {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	n := xhs.ParseCount(s)
	return &n
}

// scoreOf 热度分 = 点赞 + 收藏（收藏未知时只算点赞）；点赞未知时为 nil。
func scoreOf(m Metrics) *int {
	if m.Likes == nil {
		return nil
	}
	s := *m.Likes
	if m.Collects != nil {
		s += *m.Collects
	}
	return &s
}

func rfc3339(t time.Time) *string {
	s := t.UTC().Format(time.RFC3339)
	return &s
}

// CardItem 搜索结果卡片 → Item。卡片没有正文；发布时间由笔记 ID 推算。
func CardItem(card xhs.Card, now time.Time) Item {
	it := Item{
		Channel: Name,
		ID:      card.ID,
		URL:     xhs.NoteURL(card.ID, card.XsecToken, xhs.SourceSearch),
		Title:   card.Title,
		Author:  Author{ID: card.UserID, Name: card.Nickname},
		Metrics: Metrics{Likes: count(card.Likes), Comments: count(card.Comments), Collects: count(card.Collects)},
		Extra:   map[string]any{"detail": false},
	}
	if card.UserID != "" {
		it.Author.URL = xhs.ProfileURL(card.UserID, card.XsecToken)
	}
	if card.Type != "" {
		it.Extra["note_type"] = card.Type
	}
	if t, ok := xhs.NoteTimeFromID(card.ID, now); ok {
		it.CreatedAt = rfc3339(t)
		it.Extra["created_at_source"] = TimeFromNoteID
	}
	it.Score = scoreOf(it.Metrics)
	return it
}

// ApplyNote 用打开笔记读到的详情补全 Item：正文、准确的发布时间、完整的互动数、标签、IP 属地。
func ApplyNote(it *Item, n *xhs.Note) {
	if n == nil {
		return
	}
	if it.Title == "" {
		it.Title = n.Title
	}
	it.Text = n.Desc
	if n.XsecToken != "" {
		it.URL = xhs.NoteURL(it.ID, n.XsecToken, xhs.SourceSearch)
	}
	if n.User.UserID != "" {
		it.Author.ID = n.User.UserID
	}
	if name := n.User.Name(); name != "" {
		it.Author.Name = name
	}
	switch {
	case n.AuthorHref != "":
		it.Author.URL = n.AuthorHref
	case it.Author.ID != "":
		it.Author.URL = xhs.ProfileURL(it.Author.ID, n.XsecToken)
	}
	ii := n.InteractInfo
	if v := count(ii.LikedCount); v != nil {
		it.Metrics.Likes = v
	}
	if v := count(ii.CommentCount); v != nil {
		it.Metrics.Comments = v
	}
	if v := count(ii.CollectedCount); v != nil {
		it.Metrics.Collects = v
	}
	it.Score = scoreOf(it.Metrics)
	if n.Time > 0 {
		it.CreatedAt = rfc3339(time.UnixMilli(n.Time))
		it.Extra["created_at_source"] = TimeFromDetail
	}
	it.Extra["detail"] = true
	if tags := n.Tags(); len(tags) > 0 {
		it.Extra["tags"] = tags
	}
	if n.IPLocation != "" {
		it.Extra["ip_location"] = n.IPLocation
	}
	if n.Type != "" {
		it.Extra["note_type"] = n.Type
	}
	if v := count(ii.ShareCount); v != nil {
		it.Extra["shares"] = *v
	}
}

// createdAt Item 的发布时间；未知返回 false。
func createdAt(it Item) (time.Time, bool) {
	if it.CreatedAt == nil {
		return time.Time{}, false
	}
	t, err := time.Parse(time.RFC3339, *it.CreatedAt)
	return t, err == nil
}

// FilterSince 去掉早于 now - hours 的条目；hours <= 0 不过滤。发布时间未知的条目保留，返回保留下来的未知条数。
func FilterSince(items []Item, hours float64, now time.Time) (out []Item, unknown int) {
	if hours <= 0 {
		return items, 0
	}
	cutoff := now.Add(-time.Duration(hours * float64(time.Hour)))
	out = items[:0:0]
	for _, it := range items {
		t, ok := createdAt(it)
		if !ok {
			unknown++
			out = append(out, it)
			continue
		}
		if !t.Before(cutoff) {
			out = append(out, it)
		}
	}
	return out, unknown
}

// SortItems 本地重排（稳定排序）：time 按发布时间新→旧，score 按热度分高→低，未知的排在最后；relevance 保持页面顺序。
func SortItems(items []Item, by string) {
	switch by {
	case SortTime:
		sort.SliceStable(items, func(i, j int) bool {
			ti, oki := createdAt(items[i])
			tj, okj := createdAt(items[j])
			if oki != okj {
				return oki
			}
			return ti.After(tj)
		})
	case SortScore:
		sort.SliceStable(items, func(i, j int) bool {
			si, sj := items[i].Score, items[j].Score
			if (si != nil) != (sj != nil) {
				return si != nil
			}
			return si != nil && *si > *sj
		})
	}
}
