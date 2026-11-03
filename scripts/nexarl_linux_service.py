#!/usr/bin/env python3
"""Run the Linux review console using its saved loopback port."""
import os
import vibe_memory_paths
import vibe_memory_settings


def main():
    settings = vibe_memory_settings.load_settings(vibe_memory_paths.for_home())
    os.environ['MEMORY_REVIEW_HOST'] = '127.0.0.1'
    os.environ['MEMORY_REVIEW_PORT'] = str(settings['service_port'])
    import memory_review_server
    return memory_review_server.main()


if __name__ == '__main__':
    raise SystemExit(main())