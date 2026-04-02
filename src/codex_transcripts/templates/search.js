(function() {
  var totalPages = {{ total_pages }};
  var searchBox = document.getElementById('search-box');
  var searchInput = document.getElementById('search-input');
  var searchBtn = document.getElementById('search-btn');
  var modal = document.getElementById('search-modal');
  var modalInput = document.getElementById('modal-search-input');
  var modalSearchBtn = document.getElementById('modal-search-btn');
  var modalCloseBtn = document.getElementById('modal-close-btn');
  var searchStatus = document.getElementById('search-status');
  var searchResults = document.getElementById('search-results');
  var embeddedSearchData = Array.isArray(window.__TRANSCRIPT_SEARCH_DATA__) ? window.__TRANSCRIPT_SEARCH_DATA__ : [];
  var isFileProtocol = window.location.protocol === 'file:';

  if (!searchBox || !modal || !searchInput || !searchBtn || !modalInput || !modalSearchBtn || !modalCloseBtn) {
    return;
  }

  if (isFileProtocol && !embeddedSearchData.length) {
    return;
  }

  searchBox.style.display = 'flex';

  var hostname = window.location.hostname;
  var isGistPreview = hostname === 'gisthost.github.io' || hostname === 'gistpreview.github.io';
  var gistId = null;
  var gistOwner = null;
  var gistInfoLoaded = false;

  if (isGistPreview) {
    var gistMatch = window.location.search.match(/^\?([^/]+)/);
    if (gistMatch) {
      gistId = gistMatch[1];
    }
  }

  async function loadGistInfo() {
    if (!isGistPreview || !gistId || gistInfoLoaded) {
      return;
    }
    try {
      var response = await fetch('https://api.github.com/gists/' + gistId);
      if (response.ok) {
        var info = await response.json();
        gistOwner = info.owner && info.owner.login ? info.owner.login : null;
        gistInfoLoaded = true;
      }
    } catch (error) {
      console.error('Failed to load gist info:', error);
    }
  }

  function getPageFetchUrl(pageFile) {
    if (isGistPreview && gistOwner && gistId) {
      return 'https://gist.githubusercontent.com/' + gistOwner + '/' + gistId + '/raw/' + pageFile;
    }
    return pageFile;
  }

  function getPageLinkUrl(pageFile) {
    if (isGistPreview && gistId) {
      return '?' + gistId + '/' + pageFile;
    }
    return pageFile;
  }

  function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function escapeRegex(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function openModal(query) {
    modalInput.value = query || '';
    searchResults.innerHTML = '';
    searchStatus.textContent = '';
    modal.showModal();
    modalInput.focus();
    if (query) {
      performSearch(query);
    }
  }

  function closeModal() {
    modal.close();
    if (window.location.hash.startsWith('#search=')) {
      history.replaceState(null, '', window.location.pathname + window.location.search);
    }
  }

  function updateUrlHash(query) {
    if (!query) {
      return;
    }
    history.replaceState(null, '', window.location.pathname + window.location.search + '#search=' + encodeURIComponent(query));
  }

  function highlightTextNodes(element, searchTerm) {
    var walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, null, false);
    var nodesToReplace = [];

    while (walker.nextNode()) {
      var node = walker.currentNode;
      if (node.nodeValue.toLowerCase().indexOf(searchTerm.toLowerCase()) !== -1) {
        nodesToReplace.push(node);
      }
    }

    nodesToReplace.forEach(function(node) {
      var text = node.nodeValue;
      var regex = new RegExp('(' + escapeRegex(searchTerm) + ')', 'gi');
      var parts = text.split(regex);
      if (parts.length <= 1) {
        return;
      }
      var span = document.createElement('span');
      parts.forEach(function(part) {
        if (part.toLowerCase() === searchTerm.toLowerCase()) {
          var mark = document.createElement('mark');
          mark.textContent = part;
          span.appendChild(mark);
        } else if (part) {
          span.appendChild(document.createTextNode(part));
        }
      });
      node.parentNode.replaceChild(span, node);
    });
  }

  function fixInternalLinks(element, pageFile) {
    element.querySelectorAll('a[href^="#"]').forEach(function(link) {
      var href = link.getAttribute('href');
      link.setAttribute('href', pageFile + href);
    });
  }

  function processPage(pageFile, html, query) {
    var parser = new DOMParser();
    var doc = parser.parseFromString(html, 'text/html');
    var resultsFromPage = 0;

    doc.querySelectorAll('.message').forEach(function(message) {
      var text = message.textContent || '';
      if (text.toLowerCase().indexOf(query.toLowerCase()) === -1) {
        return;
      }

      resultsFromPage++;
      var msgId = message.id || '';
      var pageLinkUrl = getPageLinkUrl(pageFile);
      var link = pageLinkUrl + (msgId ? '#' + msgId : '');
      var clone = message.cloneNode(true);

      fixInternalLinks(clone, pageLinkUrl);
      highlightTextNodes(clone, query);

      var resultDiv = document.createElement('div');
      resultDiv.className = 'search-result';
      resultDiv.innerHTML = '<a href="' + link + '">' +
        '<div class="search-result-page">' + escapeHtml(pageFile) + '</div>' +
        '<div class="search-result-content">' + clone.innerHTML + '</div>' +
        '</a>';
      searchResults.appendChild(resultDiv);
    });

    return resultsFromPage;
  }

  function processEmbeddedData(query) {
    var resultsFound = 0;
    embeddedSearchData.forEach(function(item) {
      var text = item.text || '';
      if (text.toLowerCase().indexOf(query.toLowerCase()) === -1) {
        return;
      }

      resultsFound++;
      var pageLinkUrl = getPageLinkUrl(item.page);
      var link = pageLinkUrl + (item.anchor ? '#' + item.anchor : '');
      var escapedText = escapeHtml(text);
      var regex = new RegExp('(' + escapeRegex(query) + ')', 'gi');
      var highlighted = escapedText.replace(regex, '<mark>$1</mark>');

      var resultDiv = document.createElement('div');
      resultDiv.className = 'search-result';
      resultDiv.innerHTML = '<a href="' + link + '">' +
        '<div class="search-result-page">' + escapeHtml(item.page) + ' · ' + escapeHtml(item.role || '') + '</div>' +
        '<div class="search-result-content"><div class="message-content"><p>' + highlighted + '</p></div></div>' +
        '</a>';
      searchResults.appendChild(resultDiv);
    });
    return resultsFound;
  }

  async function performSearch(query) {
    if (!query.trim()) {
      searchStatus.textContent = 'Enter a search term';
      return;
    }

    updateUrlHash(query);
    searchResults.innerHTML = '';
    searchStatus.textContent = 'Searching...';

    if (isFileProtocol) {
      var embeddedResults = processEmbeddedData(query);
      searchStatus.textContent = 'Found ' + embeddedResults + ' result(s) in local transcript data';
      return;
    }

    if (isGistPreview && !gistInfoLoaded) {
      searchStatus.textContent = 'Loading gist info...';
      await loadGistInfo();
      if (!gistOwner) {
        searchStatus.textContent = 'Failed to load gist info. Search unavailable.';
        return;
      }
    }

    var resultsFound = 0;
    var pagesSearched = 0;
    var pagesToFetch = [];

    for (var i = 1; i <= totalPages; i++) {
      pagesToFetch.push('page-' + String(i).padStart(3, '0') + '.html');
    }

    var batchSize = 3;
    for (var start = 0; start < pagesToFetch.length; start += batchSize) {
      var batch = pagesToFetch.slice(start, start + batchSize);
      await Promise.all(batch.map(function(pageFile) {
        return fetch(getPageFetchUrl(pageFile))
          .then(function(response) {
            if (!response.ok) {
              throw new Error('Failed to fetch');
            }
            return response.text();
          })
          .then(function(html) {
            resultsFound += processPage(pageFile, html, query);
            pagesSearched++;
            searchStatus.textContent = 'Found ' + resultsFound + ' result(s) in ' + pagesSearched + '/' + totalPages + ' pages...';
          })
          .catch(function() {
            pagesSearched++;
            searchStatus.textContent = 'Found ' + resultsFound + ' result(s) in ' + pagesSearched + '/' + totalPages + ' pages...';
          });
      }));
    }

    searchStatus.textContent = 'Found ' + resultsFound + ' result(s) in ' + totalPages + ' pages';
  }

  searchBtn.addEventListener('click', function() {
    openModal(searchInput.value);
  });

  searchInput.addEventListener('keydown', function(event) {
    if (event.key === 'Enter') {
      openModal(searchInput.value);
    }
  });

  modalSearchBtn.addEventListener('click', function() {
    performSearch(modalInput.value);
  });

  modalInput.addEventListener('keydown', function(event) {
    if (event.key === 'Enter') {
      performSearch(modalInput.value);
    }
  });

  modalCloseBtn.addEventListener('click', function() {
    closeModal();
  });

  modal.addEventListener('cancel', function() {
    closeModal();
  });
})();
