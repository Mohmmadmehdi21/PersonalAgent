#!/usr/bin/env python3
"""sources_manager.py - CLI tool for sources.txt & rejection memory management.

Usage:
  python sources_manager.py status [--dry]
  python sources_manager.py list-removed [--dry]
  python sources_manager.py restore <video_id> [--dry] [--yes]
"""

import sys
import os
import argparse
import source_memory


def cmd_status(args):
    print("=" * 65)
    print("SOURCES.TXT & REJECTION MEMORY STATUS")
    print("=" * 65)
    stats = source_memory.get_status()
    print(f"Total lines in sources.txt:    {stats['sources_total_lines']}")
    print(f"Direct video URLs:             {stats['direct_urls']}")
    print(f"Channel discovery URLs:        {stats['channel_urls']}")
    print(f"Comment lines:                 {stats['comment_lines']}")
    print(f"Blank lines:                   {stats['blank_lines']}")
    print(f"Permanently rejected IDs:      {stats['rejected_ids_count']}")
    print(f"Guard consumed/capped videos:  {stats['guard_consumed_count']}")
    print(f"Pending usable direct URLs:    {stats['usable_direct_count']}")
    print("-" * 65)
    print("First 10 usable direct URLs:")
    for idx, u in enumerate(stats['first_10_usable'], start=1):
        print(f"  {idx:2d}. {u}")
    if not stats['first_10_usable']:
        print("  (None available)")
    print("=" * 65)


def cmd_list_removed(args):
    print("=" * 65)
    print("PERMANENTLY REJECTED / REMOVED VIDEO IDS")
    print("=" * 65)
    mem = source_memory.load_memory()
    rejected = mem.get("rejected", {})
    if not rejected:
        print("No rejected IDs found.")
        return

    print(f"{'ID':<15} {'REASON CODE':<22} {'DATE':<20} {'TITLE'}")
    print("-" * 65)
    for vid, info in sorted(rejected.items(), key=lambda x: x[1].get("timestamp", "")):
        code = info.get("reason_code", "UNKNOWN")
        ts = info.get("timestamp", "unknown")
        title = info.get("title", "(untitled)")[:25]
        print(f"{vid:<15} {code:<22} {ts:<20} {title}")
    print("=" * 65)
    print(f"Total rejected IDs: {len(rejected)}")


def cmd_restore(args):
    video_id = args.video_id.strip()
    if not video_id:
        print("[Error] Must specify video_id to restore.")
        sys.exit(1)

    mem = source_memory.load_memory()
    entry = mem.get("rejected", {}).get(video_id)
    if not entry:
        print(f"[Error] Video ID '{video_id}' is not in the rejected list.")
        sys.exit(1)

    print(f"Candidate to restore:")
    print(f"  ID:          {video_id}")
    print(f"  URL:         {entry.get('url', 'unknown')}")
    print(f"  Title:       {entry.get('title', 'unknown')}")
    print(f"  Reason:      {entry.get('reason_code', 'unknown')} - {entry.get('reason_text', '')}")
    print(f"  Origin:      {entry.get('source_origin', 'unknown')}")

    if not args.yes and not args.dry:
        try:
            confirm = input(f"\nAre you sure you want to restore video ID '{video_id}'? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("Restore cancelled.")
                return
        except EOFError:
            print("Non-interactive session detected without --yes flag. Cancelled.")
            return

    success = source_memory.restore(video_id, dry_run=args.dry)
    if success:
        if args.dry:
            print(f"[DRY-RUN] Video '{video_id}' would be restored to sources.txt and un-marked in rejection memory.")
        else:
            print(f"[Success] Video '{video_id}' restored to sources.txt and removed from rejection memory.")
    else:
        print(f"[Error] Failed to restore video '{video_id}'.")
        sys.exit(1)


def main():
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("--dry", action="store_true", help="Perform dry run without modifying files")

    parser = argparse.ArgumentParser(description="Manage sources.txt and rejection memory", parents=[parent_parser])
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Show sources and rejection memory status", parents=[parent_parser])
    p_status.set_defaults(func=cmd_status)

    # list-removed
    p_list = subparsers.add_parser("list-removed", help="List all removed/rejected video IDs", parents=[parent_parser])
    p_list.set_defaults(func=cmd_list_removed)

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore a rejected video ID", parents=[parent_parser])
    p_restore.add_argument("video_id", help="11-character YouTube video ID to restore")
    p_restore.add_argument("--yes", "-y", action="store_true", help="Confirm restore automatically")
    p_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
