<template>
  <!-- Static version label. In-place update, rollback and restart were removed on
       purpose: this deployment is upgraded by replacing the image, and the
       updater would install the stock upstream binary over the custom build. -->
  <span v-if="version" class="text-xs text-gray-500 dark:text-dark-400">
    v{{ version }}
  </span>
</template>

<script setup lang="ts">
defineProps<{
  version?: string
}>()
</script>
