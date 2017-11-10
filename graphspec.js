$(window).ready(function () {
    // Make an inverse closure, i.e. the ancestors of any node.
    graph.inverse_closure = {};
    $.each(graph.transitive_closure, function(parent, descendants) {
        $.each(descendants, function(child, _) {
            if(! graph.inverse_closure[child]) {
                graph.inverse_closure[child] = {};
            }
            graph.inverse_closure[child][parent] = true;
        });
    });

    $('g.node').click(function (e) {
        var target = e.currentTarget.id;

        // Use shift to add new highlights, without dimming the ones already highlighted
        var additive = e.shiftKey;
        // Use the alt-key to change direction, i.e. highlight ancestors instead of descendants.
        var upwards = e.altKey;
        var closure = upwards ? graph.inverse_closure : graph.transitive_closure;

        if(! additive) {
            $('.selected').removeClass('selected');
        }

        $(e.currentTarget).addClass('selected');

        $('g.node').each(function (i, node) {
            var nodeId = node.id;
            if (nodeId === target ||
                (closure[target] && closure[target][node.id])
            ) {
                $(node).removeClass("ignore").addClass("highlight");
            } else {
                if(! additive) {
                    $(node).addClass("ignore").removeClass("highlight");
                }
            }
        });

        $('g.edge').each(function (i, edge) {
            var edgeId = edge.id;
            var parts = edgeId.split("/");
            var start = parts[0];
            var end = parts[1];

            var include = (
                upwards && (end === target) || // If we're going up and the edge targets us, include it
                !upwards && (start === target) || // Conversely, if we're going down and we're the start
                (closure[target] && closure[target][start] && closure[target][end]) // The edge exists between ancestors/descendants
            );

            if(include) {
                $(edge).removeClass("ignore").addClass("highlight");
            } else {
                if(! additive) {
                    $(edge).addClass("ignore").removeClass("highlight");
                }
            }
        });

        e.preventDefault();
        return false;
    });

    // When hovering over an edge or a node, display comments if some have been provided
    $('g.node, g.edge').mouseover(function(e) {
        var target = e.currentTarget.id,
            description = $(e.currentTarget).find('a').first().attr('xlink:title'),
            header = target;

        if($(e.currentTarget).is('.edge')) {
            var parts = target.split("/");
            var start = parts[0];
            var end = parts[1];
            header = start + '&nbsp;&rarr;&nbsp;' + end;
        }
        if(! description) {
            return;
        }

        $('.hover').show();
        $('.hover .path').html(header);
        $('.hover .description').html(window.markdownit().render(description));
    });

    $('g.node, g.edge').mouseout(function(e) {
        $('.hover').hide();
    });

    if(window.localStorage.hideProfiles == "false") {
        $('.profiles').removeClass("hide");
        $('.toggle-profiles').addClass("hide");
    }
    $('.hamburger').click(function() { 
        window.localStorage.hideProfiles = ! (window.localStorage.hideProfiles == "true");

        if(window.localStorage.hideProfiles == "true") {
            $('.profiles').addClass("hide");
            $('.toggle-profiles').removeClass("hide");
        } else {
            $('.profiles').removeClass("hide");
            $('.toggle-profiles').addClass("hide");
        }
    });
});