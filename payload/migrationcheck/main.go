package main

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/repository"
	_ "github.com/lib/pq"
)

func main() {
	dsn := strings.TrimSpace(os.Getenv("PATCH_TEST_DATABASE_URL"))
	if dsn == "" {
		dsn = fmt.Sprintf(
			"host=%s port=%s user=%s password=%s dbname=%s sslmode=%s",
			env("DATABASE_HOST", "127.0.0.1"),
			env("DATABASE_PORT", "5432"),
			env("DATABASE_USER", "sub2api"),
			os.Getenv("DATABASE_PASSWORD"),
			env("DATABASE_DBNAME", "sub2api"),
			env("DATABASE_SSLMODE", "disable"),
		)
	}

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		fatal(err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Minute)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		fatal(fmt.Errorf("connect clone database: %w", err))
	}
	if err := repository.ApplyMigrations(ctx, db); err != nil {
		fatal(fmt.Errorf("apply candidate migrations: %w", err))
	}
}

func env(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
