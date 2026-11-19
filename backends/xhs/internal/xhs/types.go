package xhs

import (
	"encoding/hex"
	"errors"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"
)

var (
	// ErrVerification 被重定向到安全验证页 —— 典型风控信号，必须立刻停下冷却。
	ErrVerification = errors.New("触发小红书安全验证（风控）")
	// ErrLoginRequired 登录态失效。程序不会自动登录，需要手动执行 login。
	ErrLoginRequired = errors.New("未登录或登录态失效，请先执行 login 子命令手动登录")
	// ErrNoteUnavailable 笔记已删除/不可见。
	ErrNoteUnavailable = errors.New("笔记不可访问")
	// ErrBrowserClosed Chrome 被关闭或调试连接断开，程序无法继续。
	ErrBrowserClosed = errors.New("Chrome 已关闭或调试连接断开")
)

// Source 卡片来自哪里。
type Source string

const (
	SourceFeed   Source = "feed"
	SourceSearch Source = "search"
)

type User struct {
	UserID   string `json:"userId"`
	Nickname string `json:"nickname"`
	NickName string `json:"nickName"`
}

func (u User) Name() string {
	if u.Nickname != "" {
		return u.Nickname
	}
	return u.NickName
}

type InteractInfo struct {
	LikedCount     string `json:"likedCount"`
	CollectedCount string `json:"collectedCount"`
	CommentCount   string `json:"commentCount"`
	ShareCount     string `json:"shareCount"`
}

// rawFeed 是 __INITIAL_STATE__ 里列表项的原始结构。
type rawFeed struct {
	ID        string `json:"id"`
	XsecToken string `json:"xsecToken"`
	ModelType string `json:"modelType"`
	NoteCard  struct {
		NoteID       string       `json:"noteId"`
		DisplayTitle string       `json:"displayTitle"`
		Type         string       `json:"type"`
		User         User         `json:"user"`
		InteractInfo InteractInfo `json:"interactInfo"`
	} `json:"noteCard"`
}

// Card 列表里的一张笔记卡片。
type Card struct {
	ID        string
	XsecToken string
	Title     string
	Type      string
	UserID    string
	Nickname  string
	Likes     string
	// Collects / Comments 搜索卡片里通常没有（空串表示未知）
	Collects string
	Comments string
}

func toCards(raw []rawFeed) []Card {
	out := make([]Card, 0, len(raw))
	for _, f := range raw {
		// 列表里混有直播、热搜词等非笔记条目
		if f.ModelType != "" && f.ModelType != "note" {
			continue
		}
		id := f.ID
		if id == "" {
			id = f.NoteCard.NoteID
		}
		if id == "" {
			continue
		}
		out = append(out, Card{
			ID:        id,
			XsecToken: f.XsecToken,
			Title:     f.NoteCard.DisplayTitle,
			Type:      f.NoteCard.Type,
			UserID:    f.NoteCard.User.UserID,
			Nickname:  f.NoteCard.User.Name(),
			Likes:     f.NoteCard.InteractInfo.LikedCount,
			Collects:  f.NoteCard.InteractInfo.CollectedCount,
			Comments:  f.NoteCard.InteractInfo.CommentCount,
		})
	}
	return out
}

// Note 笔记详情。
type Note struct {
	NoteID       string       `json:"noteId"`
	XsecToken    string       `json:"xsecToken"`
	Title        string       `json:"title"`
	Desc         string       `json:"desc"`
	Type         string       `json:"type"`
	Time         int64        `json:"time"` // 毫秒
	IPLocation   string       `json:"ipLocation"`
	User         User         `json:"user"`
	InteractInfo InteractInfo `json:"interactInfo"`
	TagList      []struct {
		Name string `json:"name"`
	} `json:"tagList"`

	// AuthorHref 详情弹窗里作者链接的完整地址（自带 xsec_token），作为结果里的作者链接。
	AuthorHref string `json:"-"`
}

func (n *Note) Tags() []string {
	out := make([]string, 0, len(n.TagList))
	for _, t := range n.TagList {
		out = append(out, t.Name)
	}
	return out
}

func NoteURL(id, xsecToken string, src Source) string {
	s := "pc_feed"
	if src == SourceSearch {
		s = "pc_search"
	}
	return fmt.Sprintf("https://www.xiaohongshu.com/explore/%s?xsec_token=%s&xsec_source=%s",
		id, url.QueryEscape(xsecToken), s)
}

func ProfileURL(userID, xsecToken string) string {
	u := "https://www.xiaohongshu.com/user/profile/" + userID
	if xsecToken != "" {
		u += "?xsec_token=" + url.QueryEscape(xsecToken) + "&xsec_source=pc_note"
	}
	return u
}

// ParseCount 把 "1.2万" / "999+" / "10w" 这类计数转成整数，失败返回 0。
func ParseCount(s string) int {
	s = strings.TrimSpace(strings.TrimSuffix(s, "+"))
	mul := 1.0
	switch {
	case strings.HasSuffix(s, "万"), strings.HasSuffix(s, "w"):
		mul, s = 10000, strings.TrimRight(s, "万w")
	case strings.HasSuffix(s, "千"), strings.HasSuffix(s, "k"):
		mul, s = 1000, strings.TrimRight(s, "千k")
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0
	}
	return int(f * mul)
}

// NoteTimeFromID 从笔记 ID 推算发布时间：小红书笔记 ID 是 24 位十六进制的 ObjectId，
// 前 4 字节是创建时间（Unix 秒）。与详情页 note.time 对比过，精确到秒一致。
// 格式不符或时间明显不合理（早于 2013 年 / 晚于 now+1 天）时返回 false。
func NoteTimeFromID(id string, now time.Time) (time.Time, bool) {
	if len(id) != 24 {
		return time.Time{}, false
	}
	b, err := hex.DecodeString(id[:8])
	if err != nil {
		return time.Time{}, false
	}
	t := time.Unix(int64(uint32(b[0])<<24|uint32(b[1])<<16|uint32(b[2])<<8|uint32(b[3])), 0).UTC()
	if t.Before(time.Date(2013, 1, 1, 0, 0, 0, 0, time.UTC)) || t.After(now.Add(24*time.Hour)) {
		return time.Time{}, false
	}
	return t, true
}
