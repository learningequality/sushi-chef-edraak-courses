# sushi-chef-edraak-courses
Sushi Chef script for importing edraak-courses content from https://www.edraak.org/courses/


Tree building logic:

  - `chapter` becomes L1 topic
  - `sequential` becomes L2 topic
  - `vertical`
    - parse based on heuristics
