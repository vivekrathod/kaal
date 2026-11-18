// Safari runs this in the page being shared, before "Save to Kaal" opens, and hands what it
// returns to the extension. It only reads the page's title and address.
var PageDetails = function () {};

PageDetails.prototype = {
    run: function (parameters) {
        parameters.completionFunction({ title: document.title, address: document.URL });
    },
    finalize: function () {}
};

var ExtensionPreprocessingJS = new PageDetails();
