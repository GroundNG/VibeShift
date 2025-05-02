function generateXPathForElement(currentElement) {
    function getElementPosition(currentElement) {
        if (!currentElement.parentElement) return 0;
        const tagName = currentElement.nodeName.toLowerCase();
        const siblings = Array.from(currentElement.parentElement.children)
            .filter((sib) => sib.nodeName.toLowerCase() === tagName);
        if (siblings.length === 1) return 0;
        const index = siblings.indexOf(currentElement) + 1;
        return index;
    }
    const segments = [];
    let elementToProcess = currentElement;
    while (elementToProcess && elementToProcess.nodeType === Node.ELEMENT_NODE) {
        const position = getElementPosition(elementToProcess);
        const tagName = elementToProcess.nodeName.toLowerCase();
        const xpathIndex = position > 0 ? `[${position}]` : "";
        segments.unshift(`${tagName}${xpathIndex}`);
        const parentNode = elementToProcess.parentNode;
        if (!parentNode || parentNode.nodeType !== Node.ELEMENT_NODE) {
            elementToProcess = null;
        } else if (parentNode instanceof ShadowRoot || parentNode instanceof HTMLIFrameElement) {
            elementToProcess = null;
        } else {
            elementToProcess = parentNode;
        }
    }
    let finalPath = segments.join("/");
    if (finalPath && !finalPath.startsWith('html') && !finalPath.startsWith('/html')) {
        if (finalPath.startsWith('body')) {
            finalPath = '/html/' + finalPath;
        } else if (!finalPath.startsWith('/')) {
            finalPath = '/' + finalPath;
        }
    } else if (finalPath.startsWith('body')) {
        finalPath = '/html/' + finalPath;
    }
    return finalPath || null;
}